import time
import math
import pytest


import logging
log = logging.getLogger()
log.setLevel(logging.INFO)

from tablite import Table, get_headers
from tablite.datasets import synthetic_order_data_csv, remove_synthetic_data


@pytest.fixture(autouse=True) # this resets the HDF5 file for every test.
def refresh():
    Table.reset_storage()
    yield


def test01_timing():
    table1 = Table()
    base_data = list(range(10_000))
    table1['A'] = base_data
    table1['B'] = [v*10 for v in base_data]
    table1['C'] = [-v for v in base_data]
    start = time.time()
    big_table = table1 * 10_000  # = 100_000_000
    print(f"it took {round(time.time()-start,3)}secs to extend a table to {len(big_table):,} rows")
    start = time.time()
    _ = big_table.copy()
    print(f"it took {round(time.time()-start,3)}secs to copy {len(big_table):,} rows")
    big_table.show()
    assert len(big_table) == 10_000 * 10_000,len(big_table)

    a_preview = big_table['A', 'B', 1_000:900_000:700]
    rows = [r for r in a_preview[3:15:3].rows]
    assert rows == [[3100,31000], [5200,52000], [7300, 73000], [9400, 94000]]
    a_preview.show()
  

def test01():
    rows = 8e6
    path = synthetic_order_data_csv(rows)
    if not path.exists():
        raise FileNotFoundError(path)
    
    headers = get_headers(path)
    columns = headers[path.name][0]

    config = {
        "columns":columns, 
        "delimiter":headers['delimiter'], 
        "text_qualifier":None, 
        "newline":'\n', 
        "first_row_has_headers":True
    }

    start = time.time()
    t1 = Table.import_file(path, **config)
    end = time.time()
    print(f"import of {rows:,} rows took {round(end-start, 4)} secs.")   #  import took 135.534 secs.
    
    start = time.time()
    t2 = Table.import_file(path, **config)
    end = time.time()
    print(f"reloading an imported table took {round(end-start, 4)} secs.")  # reloading an imported table took 0.177 secs.
    t1.show()
    print("-"*120)
    t2.show()
    # +=========+=======+=============+===================+=====+===+=====+====+===+=====+=====+===================+==================+
    # |    ~    |   #   |      1      |         2         |  3  | 4 |  5  | 6  | 7 |  8  |  9  |         10        |        11        |
    # |   row   |  int  |     int     |      datetime     | int |int| int |str |str|mixed|mixed|       float       |      float       |
    # +---------+-------+-------------+-------------------+-----+---+-----+----+---+-----+-----+-------------------+------------------+
    # |0        |      1|1478158906743|2021-10-27 00:00:00|50764|  1|29990|C4-5|APP|21°  |None | 2.0434376837650046|1.3371665497020444|
    # |1        |      2|2271295805011|2021-09-13 00:00:00|50141|  0|10212|C4-5|TAE|None |None |  1.010318612835485| 20.94821610676901|
    # |2        |      3|1598726492913|2021-08-19 00:00:00|50527|  0|19416|C3-5|QPV|21°  |None |  1.463459515469516|  17.4133659842749|
    # |3        |      4|1413615572689|2021-11-05 00:00:00|50181|  1|18637|C4-2|GCL|6°   |ABC  |  2.084002469706324| 0.489481411683505|
    # |4        |      5| 245266998048|2021-09-25 00:00:00|50378|  0|29756|C5-4|LGY|6°   |XYZ  | 0.5141579343276079| 8.550780816571438|
    # |5        |      6| 947994853644|2021-10-14 00:00:00|50511|  0| 7890|C2-4|BET|0°   |XYZ  | 1.1725893606177542| 7.447314130260951|
    # |6        |      7|2230693047809|2021-10-07 00:00:00|50987|  1|26742|C1-3|CFP|0°   |XYZ  | 1.0921267279498004|11.009210185311993|
    # |...      |...    |...          |...                |...  |...|...  |... |...|...  |...  |...                |...               |
    # |7,999,993|7999994|2047223556745|2021-09-03 00:00:00|50883|  1|15687|C3-1|RFR|None |XYZ  | 1.3467185981566827|17.023443485654845|
    # |7,999,994|7999995|1814140654790|2021-08-02 00:00:00|50152|  0|16556|C4-2|WTC|None |ABC  | 1.1517593924478968| 8.201818634721487|
    # |7,999,995|7999996| 155308171103|2021-10-14 00:00:00|50008|  1|14590|C1-3|WYM|0°   |None | 2.1273836233717978|23.295943554889195|
    # |7,999,996|7999997|1620451532911|2021-12-12 00:00:00|50173|  1|20744|C2-1|ZYO|6°   |ABC  |  2.482509134693724| 22.25375464857266|
    # |7,999,997|7999998|1248987682094|2021-12-20 00:00:00|50052|  1|28298|C5-4|XAW|None |XYZ  |0.17923757926558143|23.728160892974252|
    # |7,999,998|7999999|1382206732187|2021-11-13 00:00:00|50993|  1|24832|C5-2|UDL|None |ABC  |0.08425329763360942|12.707735293126758|
    # |7,999,999|8000000| 600688069780|2021-09-28 00:00:00|50510|  0|15819|C3-4|IGY|None |ABC  |  1.066241687256579|13.862069804070295|
    # +=========+=======+=============+===================+=====+===+=====+====+===+=====+=====+===================+==================+

    # re-import bypass check
    start = time.time()
    t3 = Table.import_file(path, **config)
    end = time.time()
    print(f"reloading an already imported table took {round(end-start, 4)} secs.")  #reloading an already imported table took 0.179 secs.
    t3.show(slice(3,100,17))

    # 1 TB file test
    file_size = path.stat().st_size
    one_tb = 1e12
    repetitions = math.ceil(one_tb / file_size)
    start = time.perf_counter()
    t4 = t1 * repetitions
    end = time.perf_counter()
    assert len(t4) == repetitions * len(t1)
    print(f"1 TB of data == {len(t4)} rows ({round(end-start,4)} secs)")
    t4.show()

    # finally clean up the drive:
    remove_synthetic_data()

