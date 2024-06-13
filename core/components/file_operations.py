import pandas as pd

def upload_file(file_content):
    """
    上传文件
    """
    if file_content is not None:
        data = pd.read_csv(file_content)
        return data
    return None
