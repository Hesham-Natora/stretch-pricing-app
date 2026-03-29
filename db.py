import os
from contextlib import contextmanager
from dotenv import load_dotenv
import psycopg2
from psycopg2.pool import SimpleConnectionPool
from psycopg2 import InterfaceError, OperationalError
import time


load_dotenv()

DB_URL = os.getenv("DATABASE_URL")
if not DB_URL:
    raise RuntimeError("DATABASE_URL is not set in .env")

# نعمل pool واحد على مستوى التطبيق
POOL_MINCONN = 1
POOL_MAXCONN = 10

connection_pool: SimpleConnectionPool | None = None


def init_pool():
    global connection_pool
    if connection_pool is None:
        connection_pool = SimpleConnectionPool(
            POOL_MINCONN,
            POOL_MAXCONN,
            DB_URL,
        )


def get_connection(retries=3, delay=1):
    """
    نحاول نجيب connection من الـ pool مع retry لو السيرفر رفض الاتصال
    """
    if connection_pool is None:
        init_pool()

    for attempt in range(retries):
        try:
            conn = connection_pool.getconn()

            # اختبار الاتصال
            with conn.cursor() as cur:
                cur.execute("SELECT 1;")

            return conn

        except (InterfaceError, OperationalError):
            try:
                connection_pool.putconn(conn, close=True)
            except:
                pass

            if attempt < retries - 1:
                time.sleep(delay)
            else:
                raise


def put_connection(conn):
    """
    رجّع الـ connection للـ pool بعد الاستخدام.
    """
    if connection_pool is not None and conn is not None:
        try:
            connection_pool.putconn(conn)
        except InterfaceError:
            # لو المقابل أغلقها خلاص، نضمن إغلاقها
            try:
                conn.close()
            except Exception:
                pass


@contextmanager
def get_db():
    """
    context manager يرجع cursor من الـ pool، ويعمل commit/rollback بأمان،
    حتى لو الـ connection كانت مقفولة من السيرفر.
    """
    conn = get_connection()
    cur = conn.cursor()
    try:
        yield cur
        try:
            conn.commit()
        except InterfaceError:
            # لو اتقفلت فجأة، نطنّش commit
            pass
    except Exception:
        try:
            conn.rollback()
        except InterfaceError:
            # لو اتقفلت قبل rollback، برضو نكمّل
            pass
        raise
    finally:
        try:
            cur.close()
        except InterfaceError:
            pass
        put_connection(conn)
