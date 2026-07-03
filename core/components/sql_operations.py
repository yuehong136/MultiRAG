import sqlite3

import pandas as pd


def execute_sql(sql_query):
    """
    执行 SQL
    """
    conn = sqlite3.connect(':memory:')
    c = conn.cursor()
    c.execute('''CREATE TABLE employees (id int, name text, age int, department_id int)''')
    c.execute('''INSERT INTO employees VALUES (1, 'John Doe', 35, 1)''')
    c.execute('''INSERT INTO employees VALUES (2, 'Jane Smith', 28, 2)''')
    conn.commit()
    result = pd.read_sql_query(sql_query, conn)
    return result
