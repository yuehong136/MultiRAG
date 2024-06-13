import pandas as pd

def upload_file(file_content):
    if file_content is not None:
        data = pd.read_csv(file_content)
        return data
    return None
