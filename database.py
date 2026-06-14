import sqlite3
import os

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bank.db")


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        real_name TEXT NOT NULL,
        role TEXT NOT NULL CHECK(role IN ('掌柜', '票号经办人', '复核人'))
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS bills (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        bill_no TEXT UNIQUE NOT NULL,
        amount REAL NOT NULL CHECK(amount > 0),
        issuer_id INTEGER NOT NULL,
        payee TEXT NOT NULL,
        issue_date TEXT NOT NULL,
        due_date TEXT,
        status TEXT NOT NULL DEFAULT '有效' CHECK(status IN ('有效', '已兑付', '已作废')),
        remark TEXT,
        FOREIGN KEY (issuer_id) REFERENCES users(id)
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS endorsements (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        bill_id INTEGER NOT NULL,
        endorser TEXT NOT NULL,
        endorsee TEXT NOT NULL,
        endorse_date TEXT NOT NULL,
        operator_id INTEGER NOT NULL,
        FOREIGN KEY (bill_id) REFERENCES bills(id),
        FOREIGN KEY (operator_id) REFERENCES users(id)
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS redemptions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        bill_id INTEGER NOT NULL,
        payee TEXT NOT NULL,
        amount REAL NOT NULL CHECK(amount > 0),
        request_date TEXT NOT NULL,
        operator_id INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT '待复核' CHECK(status IN ('待复核', '已完成', '已拒绝')),
        reviewer_id INTEGER,
        review_date TEXT,
        review_comment TEXT,
        FOREIGN KEY (bill_id) REFERENCES bills(id),
        FOREIGN KEY (operator_id) REFERENCES users(id),
        FOREIGN KEY (reviewer_id) REFERENCES users(id)
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS timeline (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        bill_id INTEGER NOT NULL,
        action TEXT NOT NULL,
        actor TEXT NOT NULL,
        action_date TEXT NOT NULL,
        detail TEXT,
        FOREIGN KEY (bill_id) REFERENCES bills(id)
    )
    """)

    cursor.execute("SELECT COUNT(*) FROM users")
    if cursor.fetchone()[0] == 0:
        users = [
            ("zhanggui", "王掌柜", "掌柜"),
            ("jingban1", "李经办", "票号经办人"),
            ("jingban2", "张经办", "票号经办人"),
            ("fuhe1", "赵复核", "复核人"),
            ("fuhe2", "钱复核", "复核人"),
        ]
        cursor.executemany(
            "INSERT INTO users (username, real_name, role) VALUES (?, ?, ?)", users
        )

    conn.commit()
    conn.close()


if __name__ == "__main__":
    init_db()
    print("数据库初始化完成！")
