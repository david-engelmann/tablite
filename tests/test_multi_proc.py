import hashlib
import math
import pathlib
import time
import weakref
import numpy as np
import h5py
import random
import io
import os
import json
import traceback
import queue
import multiprocessing
import copy
from multiprocessing import shared_memory

from itertools import count
from abc import ABC
from collections import defaultdict, deque

import chardet
import psutil
from pyparsing import col
from tqdm import tqdm, trange
from graph import Graph


HDF5_IMPORT_ROOT = "__h5_import"  # the hdf5 base name for imports. f.x. f['/__h5_import/column A']
MEMORY_MANAGER_CACHE_DIR = os.getcwd()
MEMORY_MANAGER_CACHE_FILE = "tablite_cache.hdf5"

class TaskManager(object):
    memory_usage_ceiling = 0.9  # 90%

    def __init__(self) -> None:
        self._memory = psutil.virtual_memory().available
        self._cpus = psutil.cpu_count()
        self._disk_space = psutil.disk_usage('/').free
        
        self.tq = multiprocessing.Queue()  # task queue for workers.
        self.rq = multiprocessing.Queue()  # result queue for workers.
        self.pool = []
        self.tasks = {}  # task register for progress tracking
        self.results = {}  # result register for progress tracking
        self._tempq = []
    
    def add(self, task):
        if not isinstance(task, Task):
            raise TypeError(f"expected instance of Task, got {type(task)}")
        self.tasks[task.tid] = task
        self._tempq.append(task)
    
    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb): # signature requires these, though I don't use them.
        self.stop()
        self.tasks.clear()
        self.results.clear()

    def start(self):
        self.pool = [Worker(name=str(i), tq=self.tq, rq=self.rq) for i in range(self._cpus)]
        for p in self.pool:
            p.start()
        while not all(p.is_alive() for p in self.pool):
            time.sleep(0.01)

    def execute(self):
        for t in self._tempq:
            self.tq.put(t)
        self._tempq = []
        with tqdm(total=len(self.tasks), unit='task') as pbar:
            while len(self.tasks) != len(self.results):
                try:
                    task = self.rq.get_nowait()
                    if task.exception: 
                        print(task)
                        raise Exception(task.exception)
                    self.results[task.tid] = task
                    pbar.update(1)
                except queue.Empty:
                    time.sleep(0.01)
                        
    def stop(self):
        self.tq.put("stop")
        while all(p.is_alive() for p in self.pool):
            time.sleep(0.01)
        print("all workers stopped")
        self.pool.clear()
  
    def chunk_size_per_cpu(self, working_memory_required):  # 39,683,483,123 = 39 Gb.
        if working_memory_required < psutil.virtual_memory().free:
            mem_per_cpu = math.ceil(working_memory_required / self._cpus)
        else:
            memory_ceiling = int(psutil.virtual_memory().total * self.memory_usage_ceiling)
            memory_used = psutil.virtual_memory().used
            available = memory_ceiling - memory_used  # 6,321,123,321 = 6 Gb
            mem_per_cpu = int(available / self._cpus)  # 790,140,415 = 0.8Gb/cpu
        return mem_per_cpu


class Worker(multiprocessing.Process):
    def __init__(self, name, tq, rq):
        super().__init__(group=None, target=self.update, name=name, daemon=False)
        self.exit = multiprocessing.Event()
        self.tq = tq  # workers task queue
        self.rq = rq  # workers result queue
        self._quit = False
        print(f"Worker-{self.name}: ready")
                
    def update(self):
        while True:
            try:
                task = self.tq.get_nowait()
            except queue.Empty:
                time.sleep(0.01)
                continue
            
            if task == "stop":
                print(f"Worker-{self.name}: stop signal received.")
                self.tq.put_nowait(task)  # this assures that everyone gets it.
                self.exit.set()
                break
            elif isinstance(task, Task):
                task.execute(f"Worker-{self.name}")
                self.rq.put(task)
            else:
                raise Exception(f"What is {task}?")


class Task(ABC):
    ids = count(start=1)
    def __init__(self, f, *args, **kwargs) -> None:
        assert callable(f)
        self._id = next(self.ids)
        self.f = f
        self.args = copy.deepcopy(args)  # deep copy is slow unless the data is shallow.
        self.kwargs = copy.deepcopy(kwargs)
        self.result = None
        self.exception = None
    @property
    def tid(self):
        return self._id

    def __str__(self) -> str:
        if self.exception:
            return f"Call to {self.f.__name__}(*{self.args}, **{self.kwargs}) --> Error: {self.exception}"
        else:
            return f"Call to{self.f.__name__}(*{self.args}, **{self.kwargs}) --> Result: {self.result}"

    def execute(self,name):
        self.name = name
        try:
            self.result = self.f(*self.args, **self.kwargs)
        except Exception as e:
            f = io.StringIO()
            traceback.print_exc(limit=3, file=f)
            f.seek(0)
            error = f.read()
            f.close()
            self.exception = error


class MemoryManager(object):
    registry = weakref.WeakValueDictionary()  # The weakref presents blocking of garbage collection.
    # Two usages:
    # {Object ID: Object} for all objects.
    # {sha256hash: Object} for DataBlocks (used to prevent duplication of data in memory.)
    lru_tracker = {}  # {DataBlockId: process_time, ...}
    map = Graph()  # Documents relations between Table, Column & Datablock.
    process_pool = None
    tasks = None
    cache_path = pathlib.Path(MEMORY_MANAGER_CACHE_DIR) / MEMORY_MANAGER_CACHE_FILE
            
    @classmethod
    def reset(cls):
        """
        enables user to erase any cached hdf5 data.
        Useful for testing where the user wants a clean working directory.

        Example:
        # new test case:
        >>> import MemoryManager
        >>> MemoryManager.reset()
        >>> ... start on testcase ...
        """
        cls.cache_file = h5py.File(cls.cache_path, mode='w')  # 'w' Create file, truncate if exists
        cls.cache_file.close()
        for obj in list(cls.registry.values()):
            del obj

    @classmethod
    def __del__(cls):
        # Use `import gc; del MemoryManager; gc.collect()` to delete the MemoryManager class.
        # shm.close()
        # shm.unlink()
        cls.cache_file.unlink()  # no loss of data.

    @classmethod
    def register(cls, obj):  # used at __init__
        assert isinstance(obj, MemoryManagedObject)
        cls.registry[obj.mem_id] = obj
        cls.lru_tracker[obj.mem_id] = time.process_time()

    @classmethod
    def deregister(cls, obj):  # used at __del__
        assert isinstance(obj, MemoryManagedObject)
        cls.registry.pop(obj.mem_id, None)
        cls.lru_tracker.pop(obj.mem_id, None)

    @classmethod
    def link(cls, a, b):
        assert isinstance(a, MemoryManagedObject)
        assert isinstance(b, MemoryManagedObject)
        
        cls.map.add_edge(a.mem_id, b.mem_id)
        if isinstance(b, DataBlock):
            # as the registry is a weakref, I need a hard ref to the datablocks!
            cls.map.add_node(b.mem_id, b)  # <-- Hard ref.

    @classmethod
    def unlink(cls, a, b):
        assert isinstance(a, MemoryManagedObject)
        assert isinstance(b, MemoryManagedObject)

        cls.map.del_edge(a.mem_id, b.mem_id)
        if isinstance(b, DataBlock):
            if cls.map.in_degree(b.mem_id) == 0:  # remove the datablock if in-degree == 0
                cls.map.del_node(b.mem_id)

    @classmethod
    def unlink_tree(cls, a):
        """
        removes `a` and descendents of `a` if descendant does not have other incoming edges.
        """
        assert isinstance(a,MemoryManagedObject)
        
        nodes = deque([a.mem_id])
        while nodes:
            n1 = nodes.popleft()
            if cls.map.in_degree(n1) == 0:
                for n2 in cls.map.nodes(from_node=n1):
                    nodes.append(n2)
                cls.map.del_node(n1)  # removes all edges automatically.
    @classmethod
    def get(cls, mem_id):
        """
        fetches datablock & maintains lru_tracker

        mem_id: DataBlocks mem_id
        returns: DataBlock
        """
        cls.lru_tracker[mem_id] = time.process_time()  # keep the lru tracker up to date.
        return cls.map.node(mem_id)
    @classmethod
    def inventory(cls):
        """
        returns printable overview of the registered tables, managed columns and datablocs.
        """
        c = count()
        node_count = len(cls.map.nodes())
        if node_count == 0:
            return "no nodes" 
        n = math.ceil(math.log10(node_count))+2
        L = []
        d = {obj.mem_id: name for name,obj in globals().copy().items() if isinstance(obj, (Table))}

        for node_id in cls.map.nodes(in_degree=0):
            name = d.get(node_id, "Table")
            obj = cls.registry.get(node_id,None)
            if obj:
                columns = [] if obj is None else list(obj.columns.keys())
                L.append(f"{next(c)}|".zfill(n) + f" {name}, columns = {columns}, registry id: {node_id}")
                for name, mc in obj.columns.items():
                    L.append(f"{next(c)}|".zfill(n) + f" └─┬ {mc.__class__.__name__} \'{name}\', length = {len(mc)}, registry id: {id(mc)}")
                    for i, block_id in enumerate(mc.order):
                        block = cls.map.node(block_id)
                        L.append(f"{next(c)}|".zfill(n) + f"   └── {block.__class__.__name__}-{i}, length = {len(block)}, registry id: {block_id}")
        return "\n".join(L)


class MemoryManagedObject(ABC):
    """
    Base Class for Memory Managed Objects
    """
    _ids = count()
    def __init__(self, mem_id) -> None:
        self._mem_id = mem_id
        MemoryManager.register(self)
    @property
    def mem_id(self):
        return self._mem_id
    @mem_id.setter
    def mem_id(self,value):
        raise AttributeError("mem_id is immutable")
    def __del__(self):
        MemoryManager.deregister(self)


class DataBlock(MemoryManagedObject):  # DataBlocks are IMMUTABLE!
    HDF5 = 'hdf5'
    SHM = 'shm'

    def __init__(self, mem_id, data=None, address=None):
        """
        mem_id: sha256sum of the datablock. Why? Because of storage.

        All datablocks are either imported or created at runtime.
        Imported datablocks reside in HDF and are immutable (otherwise you'd mess 
        with the initial state). They are stored in the users filetree.
        Datablocks created at runtime reside in the MemoryManager's 

        kwargs: (only one required)
        data: np.array
        address: tuple: 
            shared memory address: str: "psm_21467_46075"
            h5 address: tuple: ("path/to/hdf5.h5", "/table_name/column_name/sha256sum")
        """
        super().__init__(mem_id=mem_id)

        if (data is not None and address is None):
            self._type = self.SHM
                        
            if not isinstance(data, np.ndarray):
                raise TypeError("Expected a numpy array.")       

            self._handle = shared_memory.SharedMemory(create=True, size=data.nbytes)
            self._address = self._handle.name  # Example: "psm_21467_46075"

            self._data = np.ndarray(data.shape, dtype=data.dtype, buffer=self._handle.buf)  
            self._data = data[:]  # copy the source data into the shm (source may be a np.view)
            self._len = len(data)
            self._dtype = data.dtype.name

        elif (address is not None and data is None):
            self._type = self.HDF5
            if not isinstance(address, tuple) or len(address)!=2:
                raise TypeError("Expected pathlib.Path and h5 dataset address")
            path, address = address
            # Address is expected as:
            # if import: ("path/to/hdf5.h5", "/table_name/column_name/sha256sum")
            # if use_disk: ("path/to/MemoryManagers/tmp/dir", "/sha256sum")

            if not isinstance(path, pathlib.Path):
                raise TypeError(f"expected pathlib.Path, not {type(path)}")
            if not path.exists():
                raise FileNotFoundError(f"file not found: {path}")
            if not isinstance(address,str):
                raise TypeError(f"expected address as str, but got {type(address)}")
            if not address.startswith('/'):
                raise ValueError(f"address doesn't start at root.")
            
            self._handle = h5py.File(path,'r')  # imported data is immutable.
            self._address = address

            self._data = self._handle[address]            
            self._len = len(self._data)
            self._dtype = self.data.dtype.name
        else:
            raise ValueError("Either address or data must be None")

    @property
    def use_disk(self):
        return self._type == self.HDF5
    
    def use_disk(self, value):
        if value is False:
            if self._type == self.SHM:
                return  # nothing to do. Already in shm mode.
            else:  # load from hdf5 to shm
                data = self._data[:]
                self._handle = shared_memory.SharedMemory(create=True, size=data.nbytes)
                self._address = self._handle.name
                self._data = np.ndarray(data.shape, dtype=data.dtype, buffer=self._handle.buf)
                self._data = data[:] # copy the source data into the shm
                self._type = self.SHM
                return
        else:  # if value is True:
            if self._type == self.HDF5:
                return  # nothing to do. Already in HDF5 mode.
            # hdf5_name = f"{column_name}/{self.mem_id}"
            self._handle = h5py.File(MemoryManager.cache_path, 'a')
            self._address = f"/{self.sha256sum}"
            self._data = self._handle.create_dataset(self._address, data=self._data)
            
    @property
    def sha256sum(self):
        return self._mem_id
    @sha256sum.setter
    def sha256sum(self,value):
        raise AttributeError("sha256sum is immutable.")
    @property
    def address(self):
        return (self._data.shape, self._dtype, self._address)
    @property
    def data(self):
        return self._data[:]
    @data.setter
    def data(self, value):
        raise AttributeError("DataBlock.data is immutable.")
    def __len__(self) -> int:
        return self._len
    def __iter__(self):
        raise AttributeError("Use vectorised functions on DataBlock.data instead of __iter__")
    def __del__(self):
        if self._type == self.SHM:
            self._handle.close()
            self._handle.unlink()
        elif self._type == self.HDF5:
            self._handle.close()
        super().__del__()


def intercept(A,B):
    """
    enables calculation of the intercept of two range objects.
    Used to determine if a datablock contains a slice.
    
    A: range
    B: range
    
    returns: range as intercept of ranges A and B.
    """
    assert isinstance(A, range)
    if A.step < 0: # turn the range around
        A = range(A.stop, A.start, abs(A.step))
    assert isinstance(B, range)
    if B.step < 0:  # turn the range around
        B = range(B.stop, B.start, abs(B.step))
    
    boundaries = [A.start, A.stop, B.start, B.stop]
    boundaries.sort()
    a,b,c,d = boundaries
    if [A.start, A.stop] in [[a,b],[c,d]]:
        return range(0) # then there is no intercept
    # else: The inner range (subset) is b,c, limited by the first shared step.
    A_start_steps = math.ceil((b - A.start) / A.step)
    A_start = A_start_steps * A.step + A.start

    B_start_steps = math.ceil((b - B.start) / B.step)
    B_start = B_start_steps * B.step + B.start

    if A.step == 1 or B.step == 1:
        start = max(A_start,B_start)
        step = B.step if A.step==1 else A.step
        end = c
    else:
        intersection = set(range(A_start, c, A.step)).intersection(set(range(B_start, c, B.step)))
        if not intersection:
            return range(0)
        start = min(intersection)
        end = max(c, max(intersection))
        intersection.remove(start)
        step = min(intersection) - start
    
    return range(start, end, step)


class ManagedColumn(MemoryManagedObject):  # Behaves like an immutable list.
    _ids = count()
    def __init__(self) -> None:
        super().__init__(mem_id=f"MC-{next(self._ids)}")

        self.order = []  # strict order of datablocks.
        self.dtype = None
       
    def __len__(self):
        return sum(len(MemoryManager.get(block_id)) for block_id in self.order)

    def __del__(self):
        MemoryManager.unlink_tree(self)
        super().__del__()

    def __iter__(self):
        for block_id in self.order:
            datablock = MemoryManager.get(block_id)
            assert isinstance(datablock, DataBlock)
            for value in datablock.data:
                yield value

    def _normalize_slice(self, item=None):  # There's an outdated version sitting in utils.py
        """
        helper: transforms slice into range inputs
        returns start,stop,step
        """
        if item is None:
            item = slice(0, len(self), 1)
        assert isinstance(item, slice)
        
        stop = len(self) if item.stop is None else item.stop
        start = 0 if item.start is None else len(self) + item.start if item.start < 0 else item.start
        start, stop = min(start,stop), max(start,stop)
        step = 1 if item.step is None else item.step

        return start, stop, step
            
    def __getitem__(self, item):
        """
        returns a value or a ManagedColumn (slice).
        """
        if isinstance(item, slice):
            mc = ManagedColumn()  # to be returned.

            r = range(*self._normalize_slice(item))
            page_start = 0
            for block_id in self.order:
                if page_start > r.stop:
                    break
                block = MemoryManager.get(block_id)
                if page_start + len(block) < r.start:
                    page_start += len(block)
                    continue

                if r.step==1:
                    if r.start <= page_start and page_start + len(block) <= r.stop: # then we take the whole block.
                        mc.extend(block.data)
                        page_start += len(block)
                        continue
                    else:
                        pass # the block doesn't match.
                
                block_range = range(page_start, page_start+len(block))
                intercept_range = intercept(r,block_range)  # very effective!
                if len(intercept_range)==0:  # no match.
                    page_start += len(block)
                    continue

                x = {i for i in intercept_range}  # TODO: Candidate for TaskManager.
                mask = np.array([i in x for i in block_range])
                new_block = block.data[np.where(mask)]
                mc.extend(new_block)
                page_start += len(block)

            return mc
            
        elif isinstance(item, int):
            page_start = 0
            for block_id in self.order:
                block = MemoryManager.get(block_id)
                page_end = len(block)
                if page_start <= item < page_end:
                    ix = item-page_start
                    return block.data[ix]
        else:
            raise KeyError(f"{item}")

    def blocks(self):  # USEFULL FOR TASK MANAGER SO THAT TASKS ONLY ARE PERFORMED ON UNIQUE DATABLOCKs.
        """ returns the address of all blocks. """
        return [MemoryManager.get(block_id).address for block_id in self.order]           

    def _dtype_check(self, other):
        assert isinstance(other, (np.ndarray, ManagedColumn))
        if self.dtype is None:
            self.dtype = other.dtype
        elif self.dtype != other.dtype:
            raise TypeError(f"the column expects {self.dtype}, but received {other.dtype}.")
        else:
            pass

    def extend(self, data):
        """
        extends ManagedColumn with data
        """
        if isinstance(data, ManagedColumn):  # It's data we've seen before.
            self._dtype_check(data)

            self.order.extend(data.order[:])
            for block_id in data.order:
                block = MemoryManager.get(block_id)
                MemoryManager.link(self, block)
            
        else:  # It's supposedly new data.
            if not isinstance(data, np.ndarray):
                data = np.array(data)

            self._dtype_check(data)

            m = hashlib.sha256()  # let's check if it really is new data...
            m.update(data.data.tobytes())
            sha256sum = m.hexdigest()
            if sha256sum in MemoryManager.registry:  # ... not new!
                block = MemoryManager.registry.get(sha256sum)
            else:  # ... it's new!
                block = DataBlock(mem_id=sha256sum, data=data)
                MemoryManager.registry[sha256sum] = block
            # ok. solved. Now create links.
            self.order.append(block.mem_id)
            MemoryManager.link(self, block)  # Add link from Column to DataBlock
    
    def append(self, value):
        """
        Disabled. Append items is slow. Use extend on a batch instead
        """
        raise AttributeError("Append items is slow. Use extend on a batch instead")
    

class Table(MemoryManagedObject):
    _ids = count()
    def __init__(self) -> None:
        super().__init__(mem_id=f"T-{next(self._ids)}")
        self.columns = {}
    
    def __len__(self) -> int:
        if not self.columns:
            return 0
        else:
            return max(len(mc) for mc in self.columns.values())
    
    def __del__(self):
        MemoryManager.unlink_tree(self)  # columns are automatically garbage collected.
        super().__del__()

    def __getitem__(self, items):
        """
        Enables selection of columns and rows
        Examples: 

            table['a']   # selects column 'a'
            table[:10]   # selects first 10 rows from all columns
            table['a','b', slice(3:20:2)]  # selects a slice from columns 'a' and 'b'
            table['b', 'a', 'a', 'c', 2:20:3]  # selects column 'b' and 'c' and 'a' twice for a slice.

        returns values in same order as selection.
        """
        if isinstance(items, slice):
            names, slc = list(self.columns.keys()), items
        else:        
            names, slc = [], slice(len(self))
            for i in items:
                if isinstance(i,slice):
                    slc = i
                elif isinstance(i, str) and i in self.columns:
                    names.append(i)
                else:
                    raise KeyError(f"{i} is not a slice and not in column names")
        if not names:
            raise ValueError("No columns?")
        
        t = Table()
        for name in names:
            mc = self.columns[name]
            t.add_column(name, data=mc[slc])
        return t       

    def __delitem__(self, item):
        if isinstance(item, str):
            mc = self.columns[item]
            del self.columns[item]
            MemoryManager.unlink(self, mc)
            MemoryManager.unlink_tree(mc)
        elif isinstance(item, slice):
            raise AttributeError("Tables are immutable. Create a new table using filter or using an index")

    def del_column(self, name):  # alias for summetry to add_column
        self.__delitem__(name)
 
    def add_column(self, name, data):
        if name in self.columns:
            raise ValueError(f"name {name} already used")
        mc = ManagedColumn()
        mc.extend(data)
        self.columns[name] = mc
        MemoryManager.link(self, mc)  # Add link from Table to Column
    
    def __eq__(self, other) -> bool:  # TODO: Add tests for each condition.
        """
        enables comparison of self with other
        Example: TableA == TableB
        """
        if not isinstance(other, Table):
            a, b = self.__class__.__name__, other.__class__.__name__
            raise TypeError(f"cannot compare {a} with {b}")
        
        # fast simple checks.
        try:  
            self.compare(other)
        except (TypeError, ValueError):
            return False

        if len(self) != len(other):
            return False

        # the longer check.
        for name, mc in self.columns.items():
            mc2 = other.columns[name]
            if any(a!=b for a,b in zip(mc,mc2)):  # exit at the earliest possible option.
                return False
        return True

    def __iadd__(self, other):
        """ 
        enables extension of self with data from other.
        Example: Table_1 += Table_2 
        """
        self.compare(other)
        for name,mc in self.columns.items():
            mc.extend(other.columns[name])
        return self

    def __add__(self, other):
        """
        returns the joint extension of self and other
        Example:  Table_3 = Table_1 + Table_2 
        """
        self.compare(other)
        t = self.copy()
        for name,mc in other.columns.items():
            mc2 = t.columns[name]
            mc2.extend(mc)
        return t

    def stack(self,other):  # TODO: Add tests.
        """
        returns the joint stack of tables
        Example:

        | Table A|  +  | Table B| = |  Table AB |
        | A| B| C|     | A| B| D|   | A| B| C| -|
                                    | A| B| -| D|
        """
        t = self.copy()
        for name,mc2 in other.columns.items():
            if name not in t.columns:
                t.add_column(name, data=[None] * len(mc2))
            mc = t.columns[name]
            mc.extend(mc2)
        for name, mc in t.columns.items():
            if name not in other.columns:
                mc.extend(data=[None]*len(other))
        return t

    def __mul__(self, other):
        """
        enables repetition of a table
        Example: Table_x_10 = table * 10
        """
        if not isinstance(other, int):
            raise TypeError(f"repetition of a table is only supported with integers, not {type(other)}")
        t = self.copy()
        for _ in range(other-1):  # minus, because the copy is the first.
            t += self
        return t

    def compare(self,other):
        """
        compares the metadata of the two tables and raises on the first difference.
        """
        if not isinstance(other, Table):
            a, b = self.__class__.__name__, other.__class__.__name__
            raise TypeError(f"cannot compare type {b} with {a}")
        for a, b in [[self, other], [other, self]]:  # check both dictionaries.
            for name, col in a.columns.items():
                if name not in b.columns:
                    raise ValueError(f"Column {name} not in other")
                col2 = b.columns[name]
                if col.dtype != col2.dtype:
                    raise ValueError(f"Column {name}.datatype different: {col.dtype}, {col2.dtype}")
                # if col.allow_empty != col2.allow_empty:  // TODO!
                #     raise ValueError(f"Column {name}.allow_empty is different")

    def copy(self):
        """
        returns a copy of the table
        """
        t = Table()
        for name,mc in self.columns.items():
            t.add_column(name,mc)
        return t

    def rename_column(self, old, new):
        """
        renames existing column from old name to new name
        """
        if old not in self.columns:
            raise ValueError(f"'{old}' doesn't exist. See Table.columns ")
        if new in self.columns:
            raise ValueError(f"'{new}' is already in use.")

    def __iter__(self):
        """
        Disabled. Users should use Table.rows or Table.columns
        """
        raise AttributeError("use Table.rows or Table.columns")

    def __setitem__(self, key, value):
        raise TypeError(f"Use Table.add_column")

    @property
    def rows(self):
        """
        enables iteration

        for row in Table.rows:
            print(row)
        """
        generators = [iter(mc) for mc in self.columns.values()]
        for _ in range(len(self)):
            yield [next(i) for i in generators]

    def show(self, blanks=None, format='ascii'):
        """
        prints a _preview_ of the table.
        
        blanks: string to replace blanks (None is default) when shown.
        formats: 
          - 'ascii' --> ASCII (see also self.to_ascii)
          - 'md' --> markdown (see also self.to_markdown)
          - 'html' --> HTML (see also self.to_html)

        """
        converters = {
            'ascii': self.to_ascii,
            'md': self.to_markdown,
            'html': self.to_html
        }
        converter = converters.get(format, None)
        
        if converter is None:
            raise ValueError(f"format={format} not in known formats: {list(converters)}")

        if len(self) < 20:
            t = Table()
            t.add_column('#', data=[str(i) for i in range(len(self))])
            for n,mc in self.columns.items():
                t.add_column(n,data=[str(i) for i in mc])
            print(converter(t,blanks))

        else:
            t,mc,n = Table(), ManagedColumn(), len(self)
            data = [str(i) for i in range(7)] + ["..."] + [str(i) for i in range(n-7, n)]
            mc.extend(data)
            t.add_column('#', data=mc)
            for name, mc in self.columns.items():
                data = [str(i) for i in mc[:7]] + ["..."] + [str(i) for i in mc[-7:]]
                t.add_column(name, data)

        print(converter(t, blanks))

    @staticmethod
    def to_ascii(table, blanks):
        """
        enables viewing in terminals
        returns the table as ascii string
        """
        widths = {}
        names = list(table.columns)
        for name,mc in table.columns.items():
            widths[name] = max([len(name), len(str(mc.dtype))] + [len(str(v)) for v in mc])

        def adjust(v, length):
            if v is None:
                return str(blanks).ljust(length)
            elif isinstance(v, str):
                return v.ljust(length)
            else:
                return str(v).rjust(length)

        s = []
        s.append("+ " + "+".join(["=" * widths[n] for n in names]) + " +")
        s.append("| " + "|".join([n.center(widths[n], " ") for n in names]) + " |")
        s.append("| " + "|".join([str(table.columns[n].dtype).center(widths[n], " ") for n in names]) + " |")
        # s.append("| " + "|".join([str(table.columns[n].allow_empty).center(widths[n], " ") for n in names]) + " |")
        s.append("+ " + "+".join(["-" * widths[n] for n in names]) + " +")
        for row in table.rows:
            s.append("| " + "|".join([adjust(v, widths[n]) for v, n in zip(row, names)]) + " |")
        s.append("+ " + "+".join(["=" * widths[h] for h in names]) + " +")
        return "\n".join(s)

    @staticmethod
    def to_markdown(table, blanks):
        widths = {}
        names = list(table.columns)
        for name, mc in table.columns.items():
            widths[name] = max([len(name)] + [len(str(i)) for i in mc])
        
        def adjust(v, length):
            if v is None:
                return str(blanks).ljust(length)
            elif isinstance(v, str):
                return v.ljust(length)
            else:
                return str(v).rjust(length)

        s = []
        s.append("| " + "|".join([n.center(widths[n], " ") for n in names]) + " |")
        s.append("| " + "|".join(["-" * widths[n] for n in names]) + " |")
        for row in table.rows:
            s.append("| " + "|".join([adjust(v, widths[n]) for v, n in zip(row, names)]) + " |")
        return "\n".join(s)

    @staticmethod
    def to_html(table, blanks):
        raise NotImplemented("coming soon!")
           
    @classmethod
    def import_file(cls, path, 
        import_as, newline='\n', text_qualifier=None,
        delimiter=',', first_row_has_headers=True, columns=None, sheet=None):
        """
        reads path and imports 1 or more tables as hdf5

        path: pathlib.Path or str
        import_as: 'csv','xlsx','txt'                               *123
        newline: newline character '\n', '\r\n' or b'\n', b'\r\n'   *13
        text_qualifier: character: " or '                           +13
        delimiter: character: typically ",", ";" or "|"             *1+3
        first_row_has_headers: boolean                              *123
        columns: dict with column names or indices and datatypes    *123
            {'A': int, 'B': str, 'C': float, D: datetime}
            Excess column names are ignored.

        sheet: sheet name to import (e.g. 'sheet_1')                 *2
            sheets not found excess names are ignored.
            filenames will be {path}+{sheet}.h5
        
        (*) required, (+) optional, (1) csv, (2) xlsx, (3) txt, (4) h5

        TABLES FROM IMPORTED FILES ARE IMMUTABLE.
        OTHER TABLES EXIST IN MEMORY MANAGERs CACHE IF USE DISK == True
        """
        if isinstance(path, str):
            path = pathlib.Path(path)
        if not isinstance(path, pathlib.Path):
            raise TypeError(f"expected pathlib.Path, got {type(path)}")
        if not path.exists():
            raise FileNotFoundError(f"file not found: {path}")

        if not isinstance(import_as,str) and import_as in ['csv','txt','xlsx']:
            raise ValueError(f"{import_as} is not supported")
        
        # check the inputs.
        if import_as in {'xlsx'}:
            raise NotImplementedError("coming soon!")
            # 1. create a task for each the sheet.

        if import_as in {'csv', 'txt'}:
            # TODO: Check if file already has been imported.
            # TODO: Check if reimport is necessary.

            # Ok. File doesn't exist, has been changed or it's a new import config.
            with path.open('rb') as fi:
                rawdata = fi.read(10000)
                encoding = chardet.detect(rawdata)['encoding']
            
            text_escape = TextEscape(delimiter=delimiter)  # configure t.e.

            with path.open('r', encoding=encoding) as fi:
                end = find_first(fi, 0, newline)
                headers = fi.read(end)
                headers = text_escape(headers) # use t.e.
                
                if first_row_has_headers:    
                    for name in columns:
                        if name not in headers:
                            raise ValueError(f"column not found: {name}")
                else:
                    for index in columns:
                        if index not in range(len(headers)):
                            raise IndexError(f"{index} out of range({len(headers)})")
            
            file_length = path.stat().st_size  # 9,998,765,432 = 10Gb
            config = {
                'import_as': import_as,
                'path': str(path),
                'filesize': file_length,  # if this changes - re-import.
                'delimiter': delimiter,
                'columns': columns, 
                'newline': newline,
                'first_row_has_headers': first_row_has_headers,
                'text_qualifier': text_qualifier
            }

            h5 = pathlib.Path(str(path) + '.hdf5')
            skip = False
            if h5.exists():
                with h5py.File(h5,'r') as f:  # Create file, truncate if exists
                    stored_config = json.loads(f.attrs['config'])
                    for k,v in config.items():
                        if stored_config[k] != v:
                            skip = False
                            break  # set skip to false and exit for loop.
                        else:
                            skip = True
            if not skip:
                with h5py.File(h5,'w') as f:  # Create file, truncate if exists
                    f.attrs['config'] = json.dumps(config)

                with TaskManager() as tm:
                    working_overhead = 10  # random guess. TODO: Calibrate.
                    mem_per_cpu = tm.chunk_size_per_cpu(file_length * working_overhead)
                    mem_per_task = mem_per_cpu // working_overhead  # 1 Gb / 10x = 100Mb
                    tasks = math.ceil(file_length / mem_per_task)
                    
                    tr_cfg = {
                        "source":path, 
                        "destination":h5, 
                        "columns":columns, 
                        "newline":newline, 
                        "delimiter":delimiter, 
                        "first_row_has_headers":first_row_has_headers,
                        "qoute":text_qualifier,
                        "text_escape_openings":'', "text_escape_closures":'',
                        "start":None, "limit":mem_per_task,
                        "encoding":encoding
                    }

                    for i in range(tasks):
                        # add task for each chunk for working
                        tr_cfg['start'] = i * mem_per_task
                        task = Task(f=text_reader, **tr_cfg)
                        tm.add(task)
                    
                    tm.execute()
                # Merging chunks in hdf5 into single columns
                consolidate(path)  # no need to task manager as this is done using
                # virtual layouts and virtual datasets.
                
                # Finally: Calculate sha256sum.
                with h5py.File(path,'r+') as f:  # 'r+' in case the sha256sum is missing.
                    for name in f.keys():
                        if name == HDF5_IMPORT_ROOT:
                            continue
                        
                        m = hashlib.sha256()  # let's check if it really is new data...
                        dset = f[f"/{name}"]
                        step = 100_000
                        desc = f"Calculating missing sha256sum for {name}: "
                        for i in trange(0,len(dset), step, desc=desc):
                            chunk = dset[i:i+step]
                            m.update(chunk.tobytes())
                        sha256sum = m.hexdigest()
                        f[f"/{name}"].attrs['sha256sum'] = sha256sum
                print(f"Import done: {path}")
            return Table.load_file(path)

    @classmethod
    def inspect_h5_file(cls, path, group='/'):
        """
        enables inspection of contents of HDF5 file 
        path: str or pathlib.Path
        group: you can give a specific group, defaults to the root: '/'
        """
        def descend_obj(obj,sep='  ', offset=''):
            """
            Iterate through groups in a HDF5 file and prints the groups and datasets names and datasets attributes
            """
            if type(obj) in [h5py._hl.group.Group,h5py._hl.files.File]:
                if obj.attrs.keys():  
                    for k,v in obj.attrs.items():
                        print(offset, k,":",v)  # prints config
                for key in obj.keys():
                    print(offset, key,':',obj[key])  # prints groups
                    descend_obj(obj[key],sep=sep, offset=offset+sep)
            elif type(obj)==h5py._hl.dataset.Dataset:
                for key in obj.attrs.keys():
                    print(offset, key,':',obj.attrs[key])  # prints datasets.

        with h5py.File(path,'r') as f:
            print(f"{path} contents")
            descend_obj(f[group])

    @classmethod
    def load_file(cls, path):
        """
        enables loading of imported HDF5 file. 
        Import assumes that columns are in the HDF5 root as "/{column name}"

        :path: pathlib.Path
        """
        if isinstance(path, str):
            path = pathlib.Path(path)
        if not isinstance(path, pathlib.Path):
            raise TypeError(f"expected pathlib.Path, got {type(path)}")
        if not path.exists():
            raise FileNotFoundError(f"file not found: {path}")
        if not path.name.endswith(".hdf5"):
            raise TypeError(f"expected .hdf5 file, not {path.name}")
        
        # read the file and create managed columns
        # no need for task manager as this is just fetching metadata.
        t = Table()
        with h5py.File(path,'r+') as f:  # 'r+' in case the sha256sum is missing.
            for name in f.keys():
                if name == HDF5_IMPORT_ROOT:
                    continue
                sha256sum = f[f"/{name}"].attrs.get('sha256sum',None)
                if sha256sum is None:
                    raise ValueError("no sha256sum?")
                mc = ManagedColumn()
                t.columns[name] = mc
                MemoryManager.link(t, mc)
                db = DataBlock(mem_id=sha256sum, address=(path, f"/{name}"))
                mc.order.append(db)
                MemoryManager.link(mc, db)
        return t

    def to_hdf5(self, path):
        """
        creates a copy of the table as hdf5
        the hdf5 layout can be viewed using Table.inspect_h5_file(path/to.hdf5)
        """
        if isinstance(path, str):
            path = pathlib.Path(path)
        
        total = ":,".format(len(self.columns) * len(self))
        print(f"writing {total} records to {path}")

        with h5py.File(path, 'a') as f:
            with tqdm(total=len(self.columns), unit='columns') as pbar:
                n = 0
                for name, mc in self.columns.values():
                    f.create_dataset(name, data=mc[:])  # stored in hdf5 as '/name'
                    n += 1
                    pbar.update(n)
        print(f"writing {path} to HDF5 done")

# FILE READER UTILS 2.0 ----------------------------

class TextEscape(object):
    """
    enables parsing of CSV with respecting brackets and text marks.

    Example:
    text_escape = TextEscape()  # set up the instance.
    for line in somefile.readlines():
        list_of_words = text_escape(line)  # use the instance.
        ...
    """
    def __init__(self, openings='({[', closures=']})', qoute='"', delimiter=','):
        """
        As an example, the Danes and Germans use " for inches and ' for feet, 
        so we will see data that contains nail (75 x 4 mm, 3" x 3/12"), so 
        for this case ( and ) are valid escapes, but " and ' aren't.

        """
        if not isinstance(openings, str):
            raise TypeError(f"expected str, got {type(openings)}")
        if not isinstance(closures, str):
            raise TypeError(f"expected str, got {type(closures)}")
        if not isinstance(delimiter, str):
            raise TypeError(f"expected str, got {type(delimiter)}")
        if qoute in openings or qoute in closures:
            raise ValueError("It's a bad idea to have qoute character appears in openings or closures.")
        
        self.delimiter = delimiter
        self._delimiter_length = len(delimiter)
        self.openings = {c for c in openings}
        self.closures = {c for c in closures}
        self.qoute = qoute
        
        if not self.qoute:
            self.c = self._call1
        elif openings + closures == "":
            self.c = self._call2
        else:
            self.c = self._call3

    def __call__(self,s):
        return self.c(s)
       
    def _call1(self,s):  # just looks for delimiter.
        return s.split(self.delimiter)

    def _call2(self,s): # looks for qoutes.
        words = []
        qoute= False
        ix = 0
        while ix < len(s):  
            c = s[ix]
            if c == self.qoute:
                qoute = not qoute
            if qoute:
                ix += 1
                continue
            if c == self.delimiter:
                word, s = s[:ix], s[ix+self._delimiter_length:]
                words.append(word)
                ix = -1
            ix+=1
        if s:
            words.append(s)
        return words

    def _call3(self, s):  # looks for qoutes, openings and closures.
        words = []
        qoute = False
        ix,depth = 0,0
        while ix < len(s):  
            c = s[ix]

            if c == self.qoute:
                qoute = not qoute

            if qoute:
                ix+=1
                continue

            if depth == 0 and c == self.delimiter:
                word, s = s[:ix], s[ix+self._delimiter_length:]
                words.append(word)
                ix = -1
            elif c in self.openings:
                depth += 1
            elif c in self.closures:
                depth -= 1
            else:
                pass
            ix += 1

        if s:
            words.append(s)
        return words


def detect_seperator(text):
    """
    After reviewing the logic in the CSV sniffer, I concluded that all it
    really does is to look for a non-text character. As the separator is
    determined by the first line, which almost always is a line of headers,
    the text characters will be utf-8,16 or ascii letters plus white space.
    This leaves the characters ,;:| and \t as potential separators, with one
    exception: files that use whitespace as separator. My logic is therefore
    to (1) find the set of characters that intersect with ',;:|\t' which in
    practice is a single character, unless (2) it is empty whereby it must
    be whitespace.
    """
    seps = {',', '\t', ';', ':', '|'}.intersection(text)
    if not seps:
        if " " in text:
            return " "
    else:
        frq = [(text.count(i), i) for i in seps]
        frq.sort(reverse=True)  # most frequent first.
        return {k:v for k,v in frq}

def find_first(fh, start, chars):
    """
    fh: filehandle (e.g. fh = pathlib.Path.open() )
    start: fh.seek(start) integer
    c: character to search for.

    as start + chunk_size may not equal the next newline index,
    start is read as a "soft start":
    +-------+
    x       |  
    |    y->+  if the 2nd start index is y, then I seek the 
    |       |  next newline character and start after that.
    """
    c, snippet_size = 0, 1000
    fh.seek(start)
    for _ in range(1000):
        try:
            snippet = fh.read(snippet_size)  # EOFerror?
            ix = snippet.index(chars)
        except ValueError:
            c += snippet_size
            continue
        else:
            fh.seek(0)
            return start + c + ix
    raise Exception("!")
        

def text_reader(source, destination, columns, 
                newline, delimiter=',', first_row_has_headers=True, qoute='"',
                text_escape_openings='', text_escape_closures='',
                start=None, limit=None,
                encoding='utf-8'):
    """
    reads columnsname + path[start:limit] into hdf5.

    source: csv or txt file
    destination: available filename
    
    columns: column names or indices to import

    newline: '\r\n' or '\n'
    delimiter: ',' ';' or '|'
    first_row_has_headers: boolean
    text_escape_openings: str: default: "({[ 
    text_escape_closures: str: default: ]})" 

    start: integer: The first newline after the start will be start of blob.
    limit: integer: appx size of blob. The first newline after start of 
                    blob + limit will be the real end.

    encoding: chardet encoding ('utf-8, 'ascii', ..., 'ISO-22022-CN')
    root: hdf5 root, cannot be the same as a column name.
    """
    if isinstance(source, str):
        source = pathlib.Path(source)
    if not isinstance(source, pathlib.Path):
        raise TypeError
    if not source.exists():
        raise FileNotFoundError(f"File not found: {source}")

    if isinstance(destination, str):
        destination = pathlib.Path(destination)
    if not isinstance(destination, pathlib.Path):
        raise TypeError

    assert isinstance(columns, dict)

    root=HDF5_IMPORT_ROOT
    
    text_escape = TextEscape(text_escape_openings, text_escape_closures, qoute=qoute, delimiter=delimiter)

    with source.open('r', newline='', encoding=encoding) as fi:
        
        if first_row_has_headers:
            end = find_first(fi, 0, newline)
            headers = fi.read(end)
            start = len(headers) + len(newline) if start == 0 else start   # revise start for 1st slice.
            headers = text_escape(headers)
            
            indices = {name: headers.index(name) for name in columns}
        else:        
            indices = {name: int(name) for name in columns}

        if start != 0:  # find the true beginning.
            start = find_first(fi, start, newline) + len(newline)  # + newline!
        end = find_first(fi, start + limit, newline)  # find the true end ex newline.
        fi.seek(start)
        blob = fi.read(end-start)  # 1 hard iOps. Done.
        line_count = blob.count(newline) +1  # +1 because the last line will not have it's newline.

        data = {}
        for name, dtype in columns.items():
            if dtype == 'S': # np requires string lengths to be known.
                # so the data needs to be extracted before this is possible.
                # The solution is therefore to store data as python objects, using 
                # a numpy array only fore references.
                # Once all data is collected, the reference array can be converted into
                # a fixed size array of dtype 'S', so that np.str-methods can be used.
                data[name] = np.empty((line_count, ), dtype='O') 
            else:
                data[name] = np.empty((line_count, ), dtype=dtype)  # in the first attempt
                # the user declared datatype is used. Should this however fail at any time,
                # the array will be turned into 'O' type.

        for line_no, line in enumerate(blob.split(newline)):
            fields = text_escape(line)
            for name, ix in indices.items():
                try:
                    data[name][line_no] = fields[ix]
                except TypeError: # if the line above blows up, the dataset is converted
                    default_data = np.empty((line_count, ), dtype='O')   # ... to bytes
                    default_data[:] = data[name][:]                      # ... and replaced.
                    data[name] = default_data                            # this switch should only happen once per column.
                    # ^-- all the data has been copied, so finish the operation below --v
                    data[name][line_no] = fields[ix]
                except IndexError as e:
                    print(f"Found {len(fields)}, but index is {ix}")
                    fields = text_escape(line)
                    raise e
                
                except Exception as e:
                    print(f"error in {name} ({ix}) {line_no} for line\n\t{line}\nerror:")
                    raise e


        for name, dtype in columns.items():
            arr = data[name]
            if arr.dtype == 'O':
                data[name] = np.array(arr[:], dtype='S')
            arr = None

    for _ in range(100):
        try:
            with h5py.File(destination, 'a') as f:
                for name, arr in data.items():
                    f.create_dataset(f"/{root}/{name}/{start}", data=arr)  # `start` declares the slice id which order will be used for sorting
            return
        except OSError as e:
            time.sleep(random.randint(10,200)/1000)
    raise TimeoutError("Couldn't connect to OS.")


def consolidate(path):
    """
    enables consolidation of hdf5 imports from root into column named folders.
    
    path: pathlib.Path
    root: text, root for consolidation
    """
    if not isinstance(path, pathlib.Path):
        raise TypeError
    if not path.exists():
        raise FileNotFoundError(path)
    
    root=HDF5_IMPORT_ROOT

    with h5py.File(path, 'a') as f:
        if root not in f.keys():
            raise ValueError(f"hdf5 root={root} not in {f.keys()}")

        lengths = defaultdict(int)
        dtypes = defaultdict(set)  # necessary to track as data is dirty.
        for col_name in f[f"/{root}"].keys():
            for start in sorted(f[f"/{root}/{col_name}"].keys()):
                dset = f[f"/{root}/{col_name}/{start}"]
                lengths[col_name] += len(dset)
                dtypes[col_name].add(dset.dtype)
        
        if len(set(lengths.values())) != 1:
            d = {k:v for k,v in lengths.items()}
            raise ValueError(f"assymmetric dataset: {d}")
        for k,v in dtypes.items():
            if len(v) != 1:
                dtypes[k] = 'S'  # np.bytes
            else:
                dtypes[k] = v.pop()
        
        for col_name in f[root].keys():
            shape = (lengths[col_name], )
            layout = h5py.VirtualLayout(shape=shape, dtype=dtypes[col_name])
            a, b = 0, 0
            for start in sorted(f[f"{root}/{col_name}"].keys()):
                dset = f[f"{root}/{col_name}/{start}"]
                b += len(dset)
                vsource = h5py.VirtualSource(dset)
                layout[a:b] = vsource
                a = b
            f.create_virtual_dataset(f'/{col_name}', layout=layout)   

    
# TESTS -----------------
def test_range_intercept():
    A = range(500,700,3)
    B = range(520,700,3)
    C = range(10,1000,30)

    assert intercept(A,C) == range(0)
    assert set(intercept(B,C)) == set(B).intersection(set(C))

    A = range(500_000, 700_000, 1)
    B = range(10, 10_000_000, 1000)

    assert set(intercept(A,B)) == set(A).intersection(set(B))

    A = range(500_000, 700_000, 1)
    B = range(10, 10_000_000, 1)

    assert set(intercept(A,B)) == set(A).intersection(set(B))


def test_text_escape():
    # set up
    text_escape = TextEscape(openings='({[', closures=']})', qoute='"', delimiter=',')
    s = "this,is,a,,嗨,(comma,sep'd),\"text\""
    # use
    L = text_escape(s)
    assert L == ["this", "is", "a", "","嗨", "(comma,sep'd)", "\"text\""]
    

def test_basics():
    # creating a tablite incrementally is straight forward:
    table1 = Table()
    assert len(table1) == 0
    table1.add_column('A', data=[1,2,3])
    assert 'A' in table1.columns
    assert len(table1) == 3

    table1.add_column('B', data=['a','b','c'])
    assert 'B' in table1.columns
    assert len(table1) == 3

    table2 = table1.copy()

    table3 = table1 + table2
    assert len(table3) == len(table1) + len(table2)
    for row in table3.rows:
        print(row)

    tables = 3
    managed_columns_per_table = 2 
    datablocks = 2

    assert len(MemoryManager.map.nodes()) == tables + (tables * managed_columns_per_table) + datablocks
    assert len(MemoryManager.map.edges()) == tables * managed_columns_per_table + 8 - 2  # the -2 is because of double reference to 1 and 2 in Table3
    assert len(table1) + len(table2) + len(table3) == 3 + 3 + 6

    # delete table
    assert len(MemoryManager.map.nodes()) == 11, "3 tables, 6 managed columns and 2 datablocks"
    assert len(MemoryManager.map.edges()) == 12
    del table1  # removes 2 refs to ManagedColumns and 2 refs to DataBlocks
    assert len(MemoryManager.map.nodes()) == 8, "removed 1 table and 2 managed columns"
    assert len(MemoryManager.map.edges()) == 8 
    # delete column
    del table2['A']
    assert len(MemoryManager.map.nodes()) == 7, "removed 1 managed column reference"
    assert len(MemoryManager.map.edges()) == 6

    print(MemoryManager.inventory())

    del table3
    del table2
    assert len(MemoryManager.map.nodes()) == 0
    assert len(MemoryManager.map.edges()) == 0


def test_slicing():
    table1 = Table()
    base_data = list(range(10_000))
    table1.add_column('A', data=base_data)
    table1.add_column('B', data=[v*10 for v in base_data])
    table1.add_column('C', data=[-v for v in base_data])
    start = time.time()
    big_table = table1 * 10_000  # = 100_000_000
    print(f"it took {time.time()-start} to extend a table to {len(big_table)} rows")
    start = time.time()
    _ = big_table.copy()
    print(f"it took {time.time()-start} to copy {len(big_table)} rows")
    
    a_preview = big_table['A', 'B', 1_000:900_000:700]
    for row in a_preview[3:15:3].rows:
        print(row)
    a_preview.show(format='ascii')
    

def mem_test_job(shm_name, dtype, shape,index,value):
    existing_shm = shared_memory.SharedMemory(name=shm_name)
    c = np.ndarray((6,), dtype=dtype, buffer=existing_shm.buf)
    c[index] = value
    existing_shm.close()
    time.sleep(0.1)

def test_multiprocessing():
    # Create shared_memory array for workers to access.
    a = np.array([1, 1, 2, 3, 5, 8])
    shm = shared_memory.SharedMemory(create=True, size=a.nbytes)
    b = np.ndarray(a.shape, dtype=a.dtype, buffer=shm.buf)
    b[:] = a[:]

    task = Task(f=mem_test_job, shm_name=shm.name, dtype=a.dtype, shape=a.shape, index=-1, value=888)

    tasks = [task]
    for i in range(4):
        task = Task(f=mem_test_job, shm_name=shm.name, dtype=a.dtype, shape=a.shape, index=i, value=111+i)
        tasks.append(task)
        
    with TaskManager() as tm:
        # Alternative "low level usage" instead of using `with` is:
        # tm = TaskManager()
        # tm.add(task)
        # tm.start()
        # tm.execute()
        # tm.stop()

        for task in tasks:
            tm.add(task)
        tm.execute()

        for k,v in tm.results.items():
            print(k, str(v))

    print(b, f"assertion that b[-1] == 888 is {b[-1] == 888}")  
    print(b, f"assertion that b[0] == 111 is {b[0] == 111}")  
    
    shm.close()
    shm.unlink()


def test_h5_inspection():
    filename = 'a.csv.h5'

    with h5py.File(filename, 'w') as f:
        print(f.name)

        print(list(f.keys()))

        config = {
            'import_as': 'csv',
            'newline': b'\r\n',
            'text_qualifier':b'"',
            'delimiter':b",",
            'first_row_headers':True,
            'columns': {"col1": 'i8', "col2": 'int64'}
        }
        
        f.attrs['config']=str(config)
        dset = f.create_dataset("col1", dtype='i8', data=[1,2,3,4,5,6])
        dset = f.create_dataset("col2", dtype='int64', data=[5,5,5,5,5,2**33])

    # Append to dataset
    # must have chunks=True and maxshape=(None,)
    with h5py.File(filename, 'a') as f:
        dset = f.create_dataset('/sha256sum', data=[2,5,6],chunks=True, maxshape=(None, ))
        print(dset[:])
        new_data = [3,8,4]
        new_length = len(dset) + len(new_data)
        dset.resize((new_length, ))
        dset[-len(new_data):] = new_data
        print(dset[:])

        print(list(f.keys()))

    Table.inspect_h5_file(filename)
    pathlib.Path(filename).unlink()  # cleanup.


def test_file_importer():
    p = r"d:\remove_duplicates.csv"
    assert pathlib.Path(p).exists(), "?"
    p2 = pathlib.Path(p + '.hdf5')
    if p2.exists():
        p2.unlink()

    columns = {  # numpy type codes: https://numpy.org/doc/stable/reference/generated/numpy.dtype.kind.html
        'SKU ID': 'i', # integer
        'SKU description':'S', # np variable length str
        'Shipped date' : 'S', #datetime
        'Shipped time' : 'S', # integer to become time
        'vendor case weight' : 'f'  # float
    }  
    config = {
        'delimiter': ',', 
        "qoute": '"',
        "newline": "\n",
        "columns": columns, 
        "first_row_has_headers": True,
        "encoding": "ascii"
    }  

    # single processing.
    start, limit = 0, 10_000
    for _ in range(4):
        text_reader(source=p, destination=p2, start=start, limit=limit, **config)
        start = start + limit
        limit += 10_000

    consolidate(p2)
    
    Table.inspect_h5_file(p2)
    p2.unlink()  # cleanup!
    
    # now use multiprocessing
    start = time.time()
    t1 = Table.import_file(p, import_as='csv', columns=columns, delimiter=',', text_qualifier='"', newline='\n')
    end = time.time()
    print(f"import took {round(end-start)} secs.")
    start = time.time()
    t2 = Table.load(p2)
    end = time.time()
    print(f"reloading an imported table took {round(end-start),4} secs.")
    t1.show()

    p2.unlink()  # cleanup!
    




# def fx2(address):
#     shape, dtype, name = address
#     existing_shm = shared_memory.SharedMemory(name=name)
#     c = np.ndarray(shape, dtype=dtype, buffer=existing_shm.buf)
#     result = 2*c
#     existing_shm.close()  # emphasising that shm is no longer used.
#     return result

# def test_shm():   # <---- This test was failing, because pool couldn't connect to shm.
#     table1 = Table()
#     assert len(table1) == 0
#     table1.add_column('A', data=[1,2,3])  # block1
#     table1['A'].extend(data=[4,4,8])  # block2
#     table1['A'].extend(data=[8,9,10])  # block3
#     assert [v for v in table1['A']] == [1,2,3,4,4,8,8,9,10]
#     blocks = table1['A'].blocks()

#     print(blocks)
#     result = MemoryManager.process_pool.map(fx2, blocks)
#     print(result)
    


# drop datablock to hdf5
# load datablack from hdf5

# import is read csv to hdf5.
# - one file = one hdf5 file.
# - one column = one hdf5 table.

# materialize table

# multiproc
# - create join as tasklist.

# memory limit
# set task manager memory limit relative to using psutil
# update LRU cache based on access.

if __name__ == "__main__":
    # test_multiprocessing()
    test_file_importer()

    # for k,v in {k:v for k,v in sorted(globals().items()) if k.startswith('test') and callable(v)}.items():
    #     print(20 * "-" + k + "-" * 20)
    #     v()

