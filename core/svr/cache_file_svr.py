import logging
import time
import traceback

from sqlalchemy.orm import Session

from api.db.db_models import SessionLocal
from api.db.services.task_service import TaskService
from core.utils.redis_conn import REDIS_CONN
from common import settings



def collect(db: Session):
    doc_locations = TaskService.get_ongoing_doc_name(db)
    logging.debug(doc_locations)
    if len(doc_locations) == 0:
        time.sleep(1)
        return None
    return doc_locations

def main(db: Session):
    locations = collect(db)
    if not locations:
        return
    logging.info(f"TASKS: {len(locations)}")
    for kb_id, loc in locations:
        try:
            if REDIS_CONN.is_alive():
                try:
                    key = "{}/{}".format(kb_id, loc)
                    if REDIS_CONN.exist(key):
                        continue
                    file_bin = settings.STORAGE_IMPL.get(kb_id, loc)
                    REDIS_CONN.transaction(key, file_bin, 12 * 60)
                    logging.info("CACHE: {}".format(loc))
                except Exception as e:
                    logging.error(f"Error to get data from REDIS: {e}")
                    traceback.print_stack()
        except Exception as e:
            logging.error(f"Error to check REDIS connection: {e}")
            traceback.print_stack()



if __name__ == "__main__":
    while True:
        db = SessionLocal()
        main(db)
        db.colse()
        time.sleep(1)