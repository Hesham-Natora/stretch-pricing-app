from db import get_connection

def init_db():
    with open("schema.sql", "r") as f:
        sql = f.read()
    
    with get_connection() as conn:
        cur = conn.cursor()
        try:
            cur.execute(sql)
            conn.commit()
            print("✅ Database initialized!")
        except Exception as e:
            print(f"❌ Error: {e}")
            conn.rollback()
            raise
        finally:
            cur.close()

if __name__ == "__main__":
    init_db()
