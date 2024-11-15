import logging
import sys


def python_version_validation():
    # Check python version
    required_python_version = (3, 10)
    if sys.version_info < required_python_version:
        logging.info(
            f"Required Python: >= {required_python_version[0]}.{required_python_version[1]}. Current Python version: {sys.version_info[0]}.{sys.version_info[1]}."
        )
        sys.exit(1)
    else:
        logging.info(f"Python version: {sys.version_info[0]}.{sys.version_info[1]}")


python_version_validation()

# Download nltk data
def download_nltk_data():
    import nltk
    nltk.download('wordnet', halt_on_error=False, quiet=True)
    nltk.download('punkt_tab', halt_on_error=False, quiet=True)
# download_nltk_data()
logging.info(f"默认已下载 wordnet、punkt_tab ~如有下载需求请进入 api/validation.py 解开注释")