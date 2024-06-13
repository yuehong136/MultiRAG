import pandas as pd

def process_data(data, method):
    if method == "数值列求和":
        data['Sum'] = data.select_dtypes(include='number').sum(axis=1)
    elif method == "数值列求平均":
        data['Mean'] = data.select_dtypes(include='number').mean(axis=1)
    return data
