from tablite import Table

import numpy as np
from datetime import datetime, timedelta


def setup():  # pytest does this with every test.
    Table.reset_storage()

def teardown():  # pytest does this with every test.
    Table.reset_storage()


def test01():
    now = datetime.now().replace(microsecond=0)

    table4 = Table()
    table4['A'] = [-1, 1]
    table4['B'] = [None, 1]     
    table4['C'] = [-1.1, 1.1]
    table4['D'] = ["", "1000"]     
    table4['E'] = [None, "1"]   
    table4['F'] = [False, True]
    table4['G'] = [now, now]
    table4['H'] = [now.date(), now.date()]
    table4['I'] = [now.time(), now.time()]
    table4['J'] = [timedelta(1), timedelta(2, 400)]
    assert table4.columns == ['A','B','C','D','E','F','G','H','I','J']  # testing .columns property.

    table4.save = True  # testing that save keeps the data in HDF5.
    del table4  
    
    # recover all active tables from HDF5.
    tables = Table.reload_saved_tables()
    table5 = tables[0]  # this is the content of table4
    assert table5['A'] == [-1,1]  # list test
    assert table5['A'] == np.array([-1,1])  # numpy test
    assert table5['A'] == (-1,1)  # tuple test
    assert table5['A'] == [-1, 1]
    assert table5['B'] == [None, 1]     
    assert table5['C'] == [-1.1, 1.1]
    assert table5['D'] == ["", "1000"]     
    assert table5['E'] == [None, "1"]   
    assert table5['F'] == [False, True]
    assert table5['G'] == [now, now]
    assert table5['H'] == [now.date(), now.date()]
    assert table5['I'] == [now.time(), now.time()]
    assert table5['J'] == [timedelta(1), timedelta(2, 400)]



def test02():
    # check that the pages are not deleted prematurely.
    table4 = Table()
    table4['A'] = [-1, 1]
    table5 = Table()
    table5['A'] = table4['A']  
    
    del table4['A']
    assert table4.columns == []

    assert table5['A'] == [-1, 1]

    import gc
    del table4
    del table5
    gc.collect()  # pytest keeps reference to table4 & 5, so gc must be invoked.
    # alternatively the explicit call to .__del__ could be made.
    # table4.__del__()  
    # table5.__del__()

    tables = Table.reload_saved_tables()
    assert tables == []


def test03():
    table4 = Table()
    table4['A'] = [0,1,2,3]  # create page1
    table4['A'] += [4,5,6]   # append to page1 as ref count == 1.
    table4['A'][0] = [7]  # update as ref count == 1

    table4['A'] += table4['A']  # duplication of pages.
    table4['A'] += [8,9,10]  # append where ref count > 1 creates a new page.
    table4['A'][0] == 10  # unlink page 0, create new page and update record [0] with 10

    table5 = table4.copy()
    table5 += table4
    assert len(table5) == 2 * len(table4)

    table5.clear()
    assert table5['A'] == []

def test03a():
    
    table4 = Table()
    table4['A'] = L = [0, 10, 20, 3, 4, 5, 100]  # create
    assert L == [0, 10, 20, 3, 4, 5, 100]
    assert table4['A'] == L

    table4['A'][3], L[3] = 30, 30  # update
    assert L == [0, 10, 20, 30, 4, 5, 100]
    assert table4['A'] == L
    
    # operations below are handled by __getitem__ with a slice as a key.
    table4['A'][4:5], L[4:5] = [40,50], [40,50]  # update many as len(4:5)==len(B)
    assert L == [0, 10, 20, 30, 40, 50, 5, 100]
    assert table4['A'] == L
    
    table4['A'][-2:-1], L[-2:-1] = [60,70,80,90],[60,70,80,90]  # update 1, insert 3
    assert L == [0, 10, 20, 30, 40, 50, 60, 70, 80, 90, 100]
    assert table4['A'] == L
    
    table4['A'][2:4], L[2:4] = [2], [2]  # update + delete 1
    assert L == [0, 10, 2, 40, 50, 60, 70, 80, 90, 100]
    assert table4['A'] == L

    x = len(table4['A'])
    table4['A'][x:], L[len(L):] = [110], [110]  # append
    assert L == [0, 10, 2, 40, 50, 60, 70, 80, 90, 100, 110]
    assert table4['A'] == L
    
    del L[:3]
    del table4['A'][:3]
    assert L == [40, 50, 60, 70, 80, 90, 100, 110]
    assert table4['A'] == L

    L[::3] = [0,0,0]
    assert L == [0, 50, 60, 0, 80, 90, 0, 110]
    assert table4['A'] == L



def test03b():  # test special column functions.
    t = Table()
    n,m = 5,3
    t['A'] = [list(range(n))] * m
    col = t['A']
    k,v = col.histogram()
    assert len(k) == n
    assert sum(v) == sum(col)
    uq = col.unique()
    assert len(uq) == n
    assert sum(uq) == sum(range(n))
    ix = col.index()
    assert len(ix) == n


def test04():
    table4 = Table()
    table4['A', 'B', 'C'] = [ list(range(20)), [str(i) for i in range(20)], [1.1*i for i in range(20)]]  # test multiple assignment.
    
    table5 = table4 * 10
    assert len(table5) == len(table4)*10  # test __mul__

    assert table5['A'] == table5['A']  # test comparison of column.__eq__
    assert table5 == table5  # test comparison of table.__eq__

    for row in table4.rows:  # test .rows
        print(row)
    
    t = Table()
    t.add_column('row', int)
    t.add_column('A', int)
    t.add_column('B', int)
    t.add_column('C', int)
    t.add_row(1, 1, 2, 3)  # individual values
    t.add_row([2, 1, 2, 3])  # list of values
    t.add_row((3, 1, 2, 3))  # tuple of values
    t.add_row(*(4, 1, 2, 3))  # unpacked tuple
    t.add_row(row=5, A=1, B=2, C=3)   # keyword - args
    t.add_row(**{'row': 6, 'A': 1, 'B': 2, 'C': 3})  # dict / json.
    t.add_row((7, 1, 2, 3), (8, 4, 5, 6))  # two (or more) tuples.
    t.add_row([9, 1, 2, 3], [10, 4, 5, 6])  # two or more lists
    t.add_row({'row': 11, 'A': 1, 'B': 2, 'C': 3},
              {'row': 12, 'A': 4, 'B': 5, 'C': 6})  # two (or more) dicts as args.
    t.add_row(*[{'row': 13, 'A': 1, 'B': 2, 'C': 3},
                {'row': 14, 'A': 1, 'B': 2, 'C': 3}])  # list of dicts.

def test04a():
    pass  # multi processing index. with shared memory.


def test04b():
    pass  # test "stacking"


def test05():
    table4 = Table()
    txt = table4.to_ascii()
    assert txt.count('\n') == 2  # header.

    for i in range(24):
        table4['A'] += [i]
        table4['B'] += [str(i)]
        table4['C'] += [1.1*i]
        txt = table4.to_ascii()
        if i < 20:
            assert txt.count('\n') == i+2
        else:
            assert txt.count('\n') == 2 + 7 + 1 + 7  # 2 headers, 7 records, 1 x ..., 7 records.

    table4.show()  # launch the print function.

    txt = table4.to_ascii(slice(0,None,1))
    assert txt.count('\n') == 2 + 24

def test06():
    # doing lookups is supported by indexing
    table6 = Table()
    table6['A'] = ['Alice', 'Bob', 'Bob', 'Ben', 'Charlie', 'Ben','Albert']
    table6['B'] = ['Alison', 'Marley', 'Dylan', 'Affleck', 'Hepburn', 'Barnes', 'Einstein']
    table6.show()

    index = table6.index('A')  # single key.
    assert index[('Bob',)] == {1, 2}
    index2 = table6.index('A', 'B')  # multiple keys.
    assert index2[('Bob', 'Dylan')] == {2}

    table6.copy_to_clipboard()
    t = Table.copy_from_clipboard()
    t.show()

def test07():
    pass  # import data

def test08():
    pass  # filter

def test09():
    pass  # sort  - sort as it appears as string

def test10():
    pass  # join 

def test11():
    pass  # lookup

def test12():
    pass  # groupby

def test13():
    pass  # pivot table.


