def test_basic_table():
    # creating a tablite incrementally is straight forward:
    table = Table(use_disk=True)
    table.use_disk = True
    table.use_disk = False
    table.use_disk = True

    table.add_column('A', int, False)
    assert 'A' in table

    table.add_column('B', str, allow_empty=False)
    assert 'B' in table

    # appending rows is easy:
    table.add_row((1, 'hello'))
    table.add_row((2, 'world'))

    # converting to and from json is easy:
    table_as_json = table.to_json()
    table2 = Table.from_json(table_as_json)

    zipped = zlib.compress(table_as_json.encode())
    a, b = len(zipped), len(table_as_json)
    print("zipping reduces to", a, "from", b, "bytes, e.g.", round(100 * a / b, 0), "% of original")

    # copying is easy:
    table3 = table.copy()

    # and checking for headers is simple:
    assert 'A' in table
    assert 'Z' not in table

    # comparisons are straight forward:
    assert table == table2 == table3

    # even if you only want to check metadata:
    table.compare(table3)  # will raise exception if they're different.

    # append is easy as + also work:
    table3x2 = table3 + table3
    assert len(table3x2) == len(table3) * 2

    # and so does +=
    table3x2 += table3
    assert len(table3x2) == len(table3) * 3

    # type verification is included:
    try:
        table.columns['A'][0] = 'Hallo'
        assert False, "A TypeError should have been raised."
    except TypeError:
        assert True

    # updating values is familiar to any user who likes a list:
    assert 'A' in table.columns
    assert isinstance(table.columns['A'], (StoredList,list))
    last_row = -1
    table['A'][last_row] = 44
    table['B'][last_row] = "Hallo"

    assert table != table2

    # if you try to loop and forget the direction, Table will tell you
    try:
        for row in table:  # wont pass
            assert False, "not possible. Use for row in tablite.rows or for column in tablite.columns"
    except AttributeError:
        assert True

    _ = [table2.add_row(row) for row in table.rows]

    before = [r for r in table2.rows]
    assert before == [(1, 'hello'), (2, 'world'), (1, 'hello'), (44, 'Hallo')]

    # as is filtering for ALL that match:
    filter_1 = lambda x: 'llo' in x
    filter_2 = lambda x: x > 3

    after = table2.all(**{'B': filter_1, 'A': filter_2})

    assert list(after.rows) == [(44, 'Hallo')]

    # as is filtering or for ANY that match:
    after = table2.any(**{'B': filter_1, 'A': filter_2})

    assert list(after.rows) == [(1, 'hello'), (1, 'hello'), (44, 'Hallo')]

    # Imagine a tablite with columns a,b,c,d,e (all integers) like this:
    t = Table()
    for c in 'abcde':
        t.add_column(header=c, datatype=int, allow_empty=False, data=[i for i in range(5)])

    # we want to add two new columns using the functions:
    def f1(a, b, c):
        return a + b + c + 1

    def f2(b, c, d):
        return b * c * d

    # and we want to compute two new columns 'f' and 'g':
    t.add_column(header='f', datatype=int, allow_empty=False)
    t.add_column(header='g', datatype=int, allow_empty=True)

    # we can now use the filter, to iterate over the tablite:
    for row in t.filter('a', 'b', 'c', 'd'):
        a, b, c, d = row

        # ... and add the values to the two new columns
        t['f'].append(f1(a, b, c))
        t['g'].append(f2(b, c, d))

    assert len(t) == 5
    assert list(t.columns) == list('abcdefg')
    t.show()

    # slicing is easy:
    table_chunk = table2[2:4]
    assert isinstance(table_chunk, Table)

    # we will handle duplicate names gracefully.
    table2.add_column('B', int, allow_empty=True)
    assert set(table2.columns) == {'A', 'B', 'B_1'}

    # you can delete a column as key...
    del table2['B_1']
    assert set(table2.columns) == {'A', 'B'}

    # adding a computed column is easy:
    table.add_column('new column', str, allow_empty=False, data=[f"{r}" for r in table.rows])

    # part of or the whole tablite is easy:
    table.show()

    table.show('A', slice(0, 1))

    # updating a column with a function is easy:
    f = lambda x: x * 10
    table['A'] = [f(r) for r in table['A']]

    # using regular indexing will also work.
    for ix, r in enumerate(table['A']):
        table['A'][ix] = r * 10

    # and it will tell you if you're not allowed:
    try:
        f = lambda x: f"'{x} as text'"
        table['A'] = [f(r) for r in table['A']]
        assert False, "The line above must raise a TypeError"
    except TypeError as error:
        print("The error is:", str(error))

    # works with all datatypes:
    now = datetime.now()

    table4 = Table()
    table4.add_column('A', int, allow_empty=False, data=[-1, 1])
    table4.add_column('A', int, allow_empty=True, data=[None, 1])  # None!
    table4.add_column('A', float, False, data=[-1.1, 1.1])
    table4.add_column('A', str, False, data=["", "1"])  # Empty string is not a None, when dtype is str!
    table4.add_column('A', str, True, data=[None, "1"])  # Empty string is not a None, when dtype is str!
    table4.add_column('A', bool, False, data=[False, True])
    table4.add_column('A', datetime, False, data=[now, now])
    table4.add_column('A', date, False, data=[now.date(), now.date()])
    table4.add_column('A', time, False, data=[now.time(), now.time()])

    table4_json = table4.to_json()
    table5 = Table.from_json(table4_json)

    # .. to json and back.
    assert table4 == table5

    # And finally: I can add metadata:
    table5.metadata['db_mapping'] = {'A': 'customers.customer_name',
                                     'A_2': 'product.sku',
                                     'A_4': 'locations.sender'}

    # which also jsonifies without fuzz.
    table5_json = table5.to_json()
    table5_from_json = Table.from_json(table5_json)
    assert table5 == table5_from_json

def test_join():
    numbers = Table(use_disk=True)
    numbers.add_column('number', int, allow_empty=True, data=[1, 2, 3, 4, None])
    numbers.add_column('colour', str, data=['black', 'blue', 'white', 'white', 'blue'])

    letters = Table(use_disk=True)
    letters.add_column('letter', str, allow_empty=True, data=['a', 'b', 'c', 'd', None])
    letters.add_column('color', str, data=['blue', 'white', 'orange', 'white', 'blue'])

    # left join
    # SELECT number, letter FROM numbers LEFT JOIN letters ON numbers.colour == letters.color
    left_join = numbers.left_join(letters, left_keys=['colour'], right_keys=['color'], left_columns=['number'], right_columns=['letter'])
    left_join.show()
    # +======+======+
    # |number|letter|
    # | int  | str  |
    # | True | True |
    # +------+------+
    # |     1|None  |
    # |     2|a     |
    # |     2|None  |
    # |     3|b     |
    # |     3|d     |
    # |     4|b     |
    # |     4|d     |
    # |None  |a     |
    # |None  |None  |
    # +======+======+
    assert [i for i in left_join['number']] == [1, 2, 2, 3, 3, 4, 4, None, None]
    assert [i for i in left_join['letter']] == [None, 'a', None, 'b', 'd', 'b', 'd', 'a', None]

    # inner join
    # SELECT number, letter FROM numbers JOIN letters ON numbers.colour == letters.color
    inner_join = numbers.inner_join(letters, left_keys=['colour'], right_keys=['color'], left_columns=['number'], right_columns=['letter'])
    inner_join.show()
    # +======+======+
    # |number|letter|
    # | int  | str  |
    # | True | True |
    # +------+------+
    # |     2|a     |
    # |     2|None  |
    # |None  |a     |
    # |None  |None  |
    # |     3|b     |
    # |     3|d     |
    # |     4|b     |
    # |     4|d     |
    # +======+======+
    assert [i for i in inner_join['number']] == [2, 2, None, None, 3, 3, 4, 4]
    assert [i for i in inner_join['letter']] == ['a', None, 'a', None, 'b', 'd', 'b', 'd']

    # outer join
    # SELECT number, letter FROM numbers OUTER JOIN letters ON numbers.colour == letters.color
    outer_join = numbers.outer_join(letters, left_keys=['colour'], right_keys=['color'], left_columns=['number'], right_columns=['letter'])
    outer_join.show()
    # +======+======+
    # |number|letter|
    # | int  | str  |
    # | True | True |
    # +------+------+
    # |     1|None  |
    # |     2|a     |
    # |     2|None  |
    # |     3|b     |
    # |     3|d     |
    # |     4|b     |
    # |     4|d     |
    # |None  |a     |
    # |None  |None  |
    # |None  |c     |
    # +======+======+
    assert [i for i in outer_join['number']] == [1, 2, 2, 3, 3, 4, 4, None, None, None]
    assert [i for i in outer_join['letter']] == [None, 'a', None, 'b', 'd', 'b', 'd', 'a', None, 'c']

    assert left_join != inner_join
    assert inner_join != outer_join
    assert left_join != outer_join

def test_left_join():
    """ joining a table on itself. Wierd but possible. """
    numbers = Table()
    numbers.add_column('number', int, allow_empty=True, data=[1, 2, 3, 4, None])
    numbers.add_column('colour', str, data=['black', 'blue', 'white', 'white', 'blue'])

    left_join = numbers.left_join(numbers, left_keys=['colour'], right_keys=['colour'])

    assert list(left_join.rows) == [(1, 'black', 1, 'black'),
                                    (2, 'blue', 2, 'blue'),
                                    (2, 'blue', None, 'blue'),
                                    (3, 'white', 3, 'white'),
                                    (3, 'white', 4, 'white'),
                                    (4, 'white', 3, 'white'),
                                    (4, 'white', 4, 'white'),
                                    (None, 'blue', 2, 'blue'),
                                    (None, 'blue', None, 'blue')]


def test_left_join2():
    """ joining a table on itself. Wierd but possible. """
    numbers = Table()
    numbers.add_column('number', int, allow_empty=True, data=[1, 2, 3, 4, None])
    numbers.add_column('colour', str, data=['black', 'blue', 'white', 'white', 'blue'])

    left_join = numbers.left_join(numbers, left_keys=['colour'], right_keys=['colour'], left_columns=['colour', 'number'], right_columns=['number', 'colour'])

    assert list(left_join.rows) == [('black', 1, 1, 'black'),
                                    ('blue', 2, 2, 'blue'),
                                    ('blue', 2, None, 'blue'),
                                    ('white', 3, 3, 'white'),
                                    ('white', 3, 4, 'white'),
                                    ('white', 4, 3, 'white'),
                                    ('white', 4, 4, 'white'),
                                    ('blue', None, 2, 'blue'),
                                    ('blue', None, None, 'blue')]


def _join_left(pairs_1, pairs_2, pairs_ans, column_1, column_2):
    """
    SELECT tbl1.number, tbl1.color, tbl2.number, tbl2.color
      FROM `tbl2`
      LEFT JOIN `tbl2`
        ON tbl1.color = tbl2.color;
    """
    numbers_1 = Table()
    numbers_1.add_column('number', int, allow_empty=True)
    numbers_1.add_column('colour', str)
    for row in pairs_1:
        numbers_1.add_row(row)

    numbers_2 = Table()
    numbers_2.add_column('number', int, allow_empty=True)
    numbers_2.add_column('colour', str)
    for row in pairs_2:
        numbers_2.add_row(row)

    left_join = numbers_1.left_join(numbers_2, left_keys=[column_1], right_keys=[column_2], left_columns=['number','colour'], right_columns=['number','colour'])

    assert len(pairs_ans) == len(left_join)
    for a, b in zip(sorted(pairs_ans, key=lambda x: str(x)), sorted(list(left_join.rows), key=lambda x: str(x))):
        assert a == b


def test_same_join_1():
    """FIDDLE: http://sqlfiddle.com/#!9/7dd756/7"""

    pairs_1 = [
        (1, 'black'),
        (2, 'blue'),
        (2, 'blue'),
        (3, 'white'),
        (3, 'white'),
        (4, 'white'),
        (4, 'white'),
        (None, 'blue'),
        (None, 'blue')
    ]
    pairs_2 = [
        (1, 'black'),
        (2, 'blue'),
        (None, 'blue'),
        (3, 'white'),
        (4, 'white'),
        (3, 'white'),
        (4, 'white'),
        (2, 'blue'),
        (None, 'blue')
    ]
    pairs_ans = [
        (1, 'black', 1, 'black'),
        (2, 'blue', 2, 'blue'),
        (2, 'blue', 2, 'blue'),
        (2, 'blue', None, 'blue'),
        (2, 'blue', None, 'blue'),
        (3, 'white', 3, 'white'),
        (3, 'white', 3, 'white'),
        (3, 'white', 4, 'white'),
        (3, 'white', 4, 'white'),
        (3, 'white', 3, 'white'),
        (3, 'white', 3, 'white'),
        (3, 'white', 4, 'white'),
        (3, 'white', 4, 'white'),
        (2, 'blue', 2, 'blue'),
        (2, 'blue', 2, 'blue'),
        (2, 'blue', None, 'blue'),
        (2, 'blue', None, 'blue'),
        (None, 'blue', 2, 'blue'),
        (None, 'blue', 2, 'blue'),
        (None, 'blue', None, 'blue'),
        (None, 'blue', None, 'blue'),
        (4, 'white', 3, 'white'),
        (4, 'white', 3, 'white'),
        (4, 'white', 4, 'white'),
        (4, 'white', 4, 'white'),
        (4, 'white', 3, 'white'),
        (4, 'white', 3, 'white'),
        (4, 'white', 4, 'white'),
        (4, 'white', 4, 'white'),
        (None, 'blue', 2, 'blue'),
        (None, 'blue', 2, 'blue'),
        (None, 'blue', None, 'blue'),
        (None, 'blue', None, 'blue'),
    ]

    _join_left(pairs_1, pairs_2, pairs_ans, 'colour', 'colour')


def test_left_join_2():
    """FIDDLE: http://sqlfiddle.com/#!9/986b2a/3"""

    pairs_1 = [(1, 'black'), (2, 'blue'), (3, 'white'), (4, 'white'), (None, 'blue')]
    pairs_ans = [
        (1, 'black', 1, 'black'),
        (2, 'blue', 2, 'blue'),
        (None, 'blue', 2, 'blue'),
        (3, 'white', 3, 'white'),
        (4, 'white', 3, 'white'),
        (3, 'white', 4, 'white'),
        (4, 'white', 4, 'white'),
        (2, 'blue', None, 'blue'),
        (None, 'blue', None, 'blue'),
    ]
    _join_left(pairs_1, pairs_1, pairs_ans, 'colour', 'colour')


def test_lookup_with_all():
    tbl_0, tbl_1 = Table(), Table()
    for i, tbl in enumerate([tbl_0, tbl_1]):
        tbl.add_column("Index", int)
        tbl.add_column("Name", str)
        tbl.add_column("SKU", int)
        tbl.add_row((1 - (5 * i), "Table%i" % i, 1))
        tbl.add_row((2 - (5 * i), "Table%i" % i, 2))
        tbl.add_row((3 - (5 * i), "Table%i" % i, 3))
    tbl_0.add_row((4, "Table0", 42))
    tbl_1.add_row((-1, "Table1", 13))
    tbl_0.show()
    tbl_1.show()

    def fn_eq(a, b):
        return a == b

    def fn_neq(a, b):
        return a != b

    tbl_lookup = tbl_0.lookup(tbl_1, ("SKU", fn_eq, "SKU"), ("Index", fn_neq, "Index"))
    assert list(tbl_lookup.rows) == [(1, 'Table0', 1, -4, 'Table1', 1),
                                     (2, 'Table0', 2, -3, 'Table1', 2),
                                     (3, 'Table0', 3, -2, 'Table1', 3),
                                     (4, 'Table0', 42, None, None, None)]


def test_lookup_with_any():
    def fizz(b):
        return "fizz" if b % 3 == 0 else str(b)

    def buzz(b):
        return "buzz" if b % 5 == 0 else str(b)

    table1 = Table()
    table1.add_column('A', int, data=[i for i in range(20)])
    table1.add_column('Fizz', str, data=[fizz(i) for i in range(20)])
    table1.add_column('Buzz', str, data=[buzz(i) for i in range(20)])
    table1.show()

    table2 = Table()
    table2.add_column('B', str, data=[str(i) for i in range(20)])

    table3 = table2.lookup(table1, ("B", "==", "Fizz"), ("B", "==", "Buzz"), all=False)
    assert list(table3.rows) == [('0', None, None, None),
                                 ('1', 1, '1', '1'),
                                 ('2', 2, '2', '2'),
                                 ('3', 3, 'fizz', '3'),
                                 ('4', 4, '4', '4'),
                                 ('5', 5, '5', 'buzz'),
                                 ('6', 6, 'fizz', '6'),
                                 ('7', 7, '7', '7'),
                                 ('8', 8, '8', '8'),
                                 ('9', 9, 'fizz', '9'),
                                 ('10', 10, '10', 'buzz'),
                                 ('11', 11, '11', '11'),
                                 ('12', 12, 'fizz', '12'),
                                 ('13', 13, '13', '13'),
                                 ('14', 14, '14', '14'),
                                 ('15', None, None, None),
                                 ('16', 16, '16', '16'),
                                 ('17', 17, '17', '17'),
                                 ('18', 18, 'fizz', '18'),
                                 ('19', 19, '19', '19')]


def test_sortation():  # Sortation
    table7 = Table()
    table7.add_column('A', int, data=[1, None, 8, 3, 4, 6, 5, 7, 9], allow_empty=True)
    table7.add_column('B', int, data=[10, 100, 1, 1, 1, 1, 10, 10, 10])
    table7.add_column('C', int, data=[0, 1, 0, 1, 0, 1, 0, 1, 0])

    assert not table7.is_sorted()

    sort_order = {'B': False, 'C': False, 'A': False}

    table7.sort(**sort_order)

    assert list(table7.rows) == [
        (4, 1, 0),
        (8, 1, 0),
        (3, 1, 1),
        (6, 1, 1),
        (1, 10, 0),
        (5, 10, 0),
        (9, 10, 0),
        (7, 10, 1),
        (None, 100, 1)
    ]

    assert list(table7.filter('A', 'B', slice(4, 8))) == [(1, 10), (5, 10), (9, 10), (7, 10)]

    assert table7.is_sorted(**sort_order)



def test_lookup():
    friends = Table()
    friends.add_column("name", str, data=['Alice', 'Betty', 'Charlie', 'Dorethy', 'Edward', 'Fred'])
    friends.add_column("stop", str, data=['Downtown-1', 'Downtown-2', 'Hillside View', 'Hillside Crescent', 'Downtown-2', 'Chicago'])
    friends.show()

    random.seed(11)
    table_size = 40

    times = [DataTypes.time(random.randint(21, 23), random.randint(0, 59)) for i in range(table_size)]
    stops = ['Stadium', 'Hillside', 'Hillside View', 'Hillside Crescent', 'Downtown-1', 'Downtown-2',
             'Central station'] * 2 + [f'Random Road-{i}' for i in range(table_size)]
    route = [random.choice([1, 2, 3]) for i in stops]

    bustable = Table()
    bustable.add_column("time", DataTypes.time, data=times)
    bustable.add_column("stop", str, data=stops[:table_size])
    bustable.add_column("route", int, data=route[:table_size])

    bustable.sort(**{'time': False})

    print("Departures from Concert Hall towards ...")
    bustable[:10].show()

    lookup_1 = friends.lookup(bustable, (DataTypes.time(21, 10), "<=", 'time'), ('stop', "==", 'stop'))
    lookup_1.sort(**{'time': False})
    lookup_1.show()

    expected = [
        ('Fred', 'Chicago', None, None, None),
        ('Dorethy', 'Hillside Crescent', time(23, 54), 'Hillside Crescent', 1),
        ('Betty', 'Downtown-2', time(21, 51), 'Downtown-2', 1),
        ('Edward', 'Downtown-2', time(21, 51), 'Downtown-2', 1),
        ('Charlie', 'Hillside View', time(22, 19), 'Hillside View', 2),
        ('Alice', 'Downtown-1', time(23, 12), 'Downtown-1', 3),
    ]

    for row in lookup_1.rows:
        expected.remove(row)
    assert expected == []


def test_recreate_readme_comparison():  # TODO: Use cputils for getting the memory footprint.
    try:
        import os
        import psutil
    except ImportError:
        return
    process = psutil.Process(os.getpid())
    baseline_memory = process.memory_info().rss
    from time import process_time

    from tablite import Table

    digits = 1_000_000

    records = Table()
    records.add_column('method', str)
    records.add_column('memory', int)
    records.add_column('time', float)

    records.add_row(('python', baseline_memory, 0.0))

    # Let's now use the common and convenient "row" based format:

    start = process_time()
    L = []
    for _ in range(digits):
        L.append(tuple([11 for _ in range(10)]))
    end = process_time()

    # go and check taskmanagers memory usage.
    # At this point we're using ~154.2 Mb to store 1 million lists with 10 items.
    records.add_row(('1e6 lists w. 10 integers', process.memory_info().rss - baseline_memory, round(end-start,4)))

    L.clear()

    # Let's now use a columnar format instead:
    start = process_time()
    L = [[11 for i in range(digits)] for _ in range(10)]
    end = process_time()

    # go and check taskmanagers memory usage.
    # at this point we're using ~98.2 Mb to store 10 lists with 1 million items.
    records.add_row(('10 lists with 1e6 integers', process.memory_info().rss - baseline_memory, round(end-start,4)))
    L.clear()

    # We've thereby saved 50 Mb by avoiding the overhead from managing 1 million lists.

    # Q: But why didn't I just use an array? It would have even lower memory footprint.
    # A: First, array's don't handle None's and we get that frequently in dirty csv data.
    # Second, Table needs even less memory.

    # Let's start with an array:

    import array
    start = process_time()
    L = [array.array('i', [11 for _ in range(digits)]) for _ in range(10)]
    end = process_time()
    # go and check taskmanagers memory usage.
    # at this point we're using 60.0 Mb to store 10 lists with 1 million integers.

    records.add_row(('10 lists with 1e6 integers in arrays', process.memory_info().rss - baseline_memory, round(end-start,4)))
    L.clear()

    # Now let's use Table:

    start = process_time()
    t = Table()
    for i in range(10):
        t.add_column(str(i), int, allow_empty=False, data=[11 for _ in range(digits)])
    end = process_time()

    records.add_row(('Table with 10 columns with 1e6 integers', process.memory_info().rss - baseline_memory, round(end-start,4)))

    # go and check taskmanagers memory usage.
    # At this point we're using  97.5 Mb to store 10 columns with 1 million integers.

    # Next we'll use the api `use_stored_lists` to drop to disk:
    start = process_time()
    t.use_disk = True
    end = process_time()
    records.add_row(('Table on disk with 10 columns with 1e6 integers', process.memory_info().rss - baseline_memory, round(end-start,4)))

    # go and check taskmanagers memory usage.
    # At this point we're using  24.5 Mb to store 10 columns with 1 million integers.
    # Only the metadata remains in pythons memory.

    records.show()
