import sqlite3
import os
from datetime import date

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
    CREATE TABLE IF NOT EXISTS branches (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        branch_code TEXT UNIQUE NOT NULL,
        branch_name TEXT NOT NULL,
        location TEXT,
        manager TEXT,
        contact TEXT,
        status TEXT NOT NULL DEFAULT '营业中' CHECK(status IN ('营业中', '已歇业')),
        created_date TEXT NOT NULL,
        remark TEXT
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        real_name TEXT NOT NULL,
        role TEXT NOT NULL CHECK(role IN ('总号掌柜', '分号掌柜', '票号经办人', '复核人', '稽核员')),
        branch_id INTEGER,
        password TEXT NOT NULL DEFAULT '123456',
        status TEXT NOT NULL DEFAULT '在职' CHECK(status IN ('在职', '离职')),
        created_date TEXT NOT NULL,
        FOREIGN KEY (branch_id) REFERENCES branches(id)
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS bills (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        bill_no TEXT UNIQUE NOT NULL,
        amount REAL NOT NULL CHECK(amount > 0),
        issuer_id INTEGER NOT NULL,
        issue_branch_id INTEGER,
        payee TEXT NOT NULL,
        issue_date TEXT NOT NULL,
        due_date TEXT,
        status TEXT NOT NULL DEFAULT '有效' CHECK(status IN (
            '有效', '已兑付', '已作废', '挂失', '冻结'
        )),
        remark TEXT,
        FOREIGN KEY (issuer_id) REFERENCES users(id),
        FOREIGN KEY (issue_branch_id) REFERENCES branches(id)
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
        redeem_branch_id INTEGER,
        FOREIGN KEY (bill_id) REFERENCES bills(id),
        FOREIGN KEY (operator_id) REFERENCES users(id),
        FOREIGN KEY (reviewer_id) REFERENCES users(id),
        FOREIGN KEY (redeem_branch_id) REFERENCES branches(id)
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

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS clearings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        from_branch_id INTEGER NOT NULL,
        to_branch_id INTEGER NOT NULL,
        bill_id INTEGER,
        amount REAL NOT NULL CHECK(amount > 0),
        clearing_date TEXT NOT NULL,
        clearing_type TEXT NOT NULL CHECK(clearing_type IN ('兑付清算', '往来登记')),
        status TEXT NOT NULL DEFAULT '待清算' CHECK(status IN ('待清算', '已清算', '已对账')),
        operator_id INTEGER NOT NULL,
        remark TEXT,
        created_at TEXT NOT NULL,
        FOREIGN KEY (from_branch_id) REFERENCES branches(id),
        FOREIGN KEY (to_branch_id) REFERENCES branches(id),
        FOREIGN KEY (bill_id) REFERENCES bills(id),
        FOREIGN KEY (operator_id) REFERENCES users(id)
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS credit_limits (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        target_type TEXT NOT NULL CHECK(target_type IN ('掌柜', '经办人', '分号')),
        target_id INTEGER NOT NULL,
        daily_issue_limit REAL NOT NULL DEFAULT 0 CHECK(daily_issue_limit >= 0),
        single_redeem_limit REAL NOT NULL DEFAULT 0 CHECK(single_redeem_limit >= 0),
        balance_warning REAL NOT NULL DEFAULT 0 CHECK(balance_warning >= 0),
        created_at TEXT NOT NULL,
        updated_at TEXT,
        remark TEXT
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS exception_records (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        bill_id INTEGER NOT NULL,
        exception_type TEXT NOT NULL CHECK(exception_type IN ('挂失', '冻结', '解冻', '追回', '冲正')),
        reason TEXT NOT NULL,
        operator_id INTEGER NOT NULL,
        operator_name TEXT NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY (bill_id) REFERENCES bills(id),
        FOREIGN KEY (operator_id) REFERENCES users(id)
    )
    """)

    cursor.execute("SELECT COUNT(*) as cnt FROM branches")
    if cursor.fetchone()["cnt"] == 0:
        today = date.today().isoformat()
        branches = [
            ("ZH", "日升昌总号", "山西平遥城内西大街", "王掌柜", "总号联络处", today, "总号"),
            ("BJ", "京师分号", "北京前门大街", "刘掌柜", "京师联络处", today, "京师分号"),
            ("SH", "沪上分号", "上海十六铺", "陈掌柜", "沪上联络处", today, "沪上分号"),
            ("GZ", "粤东分号", "广州十三行", "梁掌柜", "粤东联络处", today, "粤东分号"),
            ("XA", "西安分号", "西安南院门", "马掌柜", "西安联络处", today, "西安分号"),
        ]
        cursor.executemany(
            "INSERT INTO branches (branch_code, branch_name, location, manager, contact, created_date, remark) VALUES (?, ?, ?, ?, ?, ?, ?)",
            branches,
        )

    cursor.execute("SELECT COUNT(*) as cnt FROM users")
    if cursor.fetchone()["cnt"] == 0:
        today = date.today().isoformat()
        users = [
            ("zhanggui_zong", "王总掌柜", "总号掌柜", 1, today),
            ("zhanggui_bj", "刘掌柜", "分号掌柜", 2, today),
            ("zhanggui_sh", "陈掌柜", "分号掌柜", 3, today),
            ("zhanggui_gz", "梁掌柜", "分号掌柜", 4, today),
            ("zhanggui_xa", "马掌柜", "分号掌柜", 5, today),
            ("jingban1", "李经办", "票号经办人", 1, today),
            ("jingban2", "张经办", "票号经办人", 2, today),
            ("jingban3", "赵经办", "票号经办人", 3, today),
            ("fuhe1", "钱复核", "复核人", 1, today),
            ("fuhe2", "孙复核", "复核人", 1, today),
            ("jihe1", "周稽核", "稽核员", 1, today),
            ("jihe2", "吴稽核", "稽核员", 1, today),
        ]
        cursor.executemany(
            "INSERT INTO users (username, real_name, role, branch_id, created_date) VALUES (?, ?, ?, ?, ?)",
            users,
        )

    cursor.execute("SELECT COUNT(*) as cnt FROM credit_limits")
    if cursor.fetchone()["cnt"] == 0:
        today = date.today().isoformat()
        limits = [
            ("掌柜", 1, 500000, 100000, 100000, today, "总号掌柜额度"),
            ("掌柜", 2, 200000, 50000, 50000, today, "京师分号掌柜额度"),
            ("掌柜", 3, 200000, 50000, 50000, today, "沪上分号掌柜额度"),
            ("掌柜", 4, 150000, 40000, 40000, today, "粤东分号掌柜额度"),
            ("掌柜", 5, 150000, 40000, 40000, today, "西安分号掌柜额度"),
            ("经办人", 6, 100000, 30000, 30000, today, "李经办额度"),
            ("经办人", 7, 100000, 30000, 30000, today, "张经办额度"),
            ("经办人", 8, 100000, 30000, 30000, today, "赵经办额度"),
            ("分号", 2, 500000, 200000, 100000, today, "京师分号额度"),
            ("分号", 3, 500000, 200000, 100000, today, "沪上分号额度"),
            ("分号", 4, 300000, 150000, 80000, today, "粤东分号额度"),
            ("分号", 5, 300000, 150000, 80000, today, "西安分号额度"),
        ]
        cursor.executemany(
            "INSERT INTO credit_limits (target_type, target_id, daily_issue_limit, single_redeem_limit, balance_warning, created_at, remark) VALUES (?, ?, ?, ?, ?, ?, ?)",
            limits,
        )

    conn.commit()
    conn.close()
