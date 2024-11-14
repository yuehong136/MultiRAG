import sys
# from api.utils.log_utils import logger
from core.settings import cron_logger


def python_version_validation():
    # Check python version
    required_python_version = (3, 10)
    if sys.version_info < required_python_version:
        cron_logger.info(
            f"Required Python: >= {required_python_version[0]}.{required_python_version[1]}. Current Python version: {sys.version_info[0]}.{sys.version_info[1]}."
        )
        sys.exit(1)
    else:
        cron_logger.info(f"Python version: {sys.version_info[0]}.{sys.version_info[1]}")


python_version_validation()

# todo 无网络环境不执行，启动就执行过于粗暴
# # Download nltk data
# import nltk
# nltk.download('wordnet')
# nltk.download('punkt_tab')
print(f"默认已下载wordnet、punkt_tab。如有下载需求请进入api/validation.py解开注释")