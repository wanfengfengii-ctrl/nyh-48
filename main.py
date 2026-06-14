from fastapi import FastAPI, Request, Form, Depends, HTTPException, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from database import get_db, init_db
from datetime import date, timedelta
from typing import Optional
import uuid
import json

app = FastAPI(title="传统钱庄汇票协同清算与额度风控系统")
app.add_middleware(SessionMiddleware, secret_key="qianzhuang-huipiao-secret-2024")

templates = Jinja2Templates(directory="templates")

HIGH_VALUE_THRESHOLD = 10000.0

MANAGER_ROLES = ("总号掌柜", "分号掌柜")
OPERATOR_ROLES = ("总号掌柜", "分号掌柜", "票号经办人")
REVIEWER_ROLES = ("总号掌柜", "分号掌柜", "复核人")
AUDITOR_ROLES = ("稽核员",)
ALL_MANAGE_ROLES = ("总号掌柜", "分号掌柜", "票号经办人", "复核人", "稽核员")


class AuthDependency:
    def __call__(self, request: Request):
        user = request.session.get("user")
        if not user:
            raise HTTPException(status_code=303, headers={"Location": "/login"})
        return user


auth_dep = AuthDependency()


@app.on_event("startup")
def startup():
    init_db()


def get_current_payee(bill_id: int, conn=None) -> str:
    should_close = conn is None
    if conn is None:
        conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT endorsee FROM endorsements WHERE bill_id = ? ORDER BY id DESC LIMIT 1",
        (bill_id,),
    )
    row = cursor.fetchone()
    if row:
        if should_close:
            conn.close()
        return row["endorsee"]
    cursor.execute("SELECT payee FROM bills WHERE id = ?", (bill_id,))
    row = cursor.fetchone()
    if should_close:
        conn.close()
    return row["payee"] if row else ""


def add_timeline(bill_id: int, action: str, actor: str, detail: str = "", conn=None):
    should_close = conn is None
    if conn is None:
        conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO timeline (bill_id, action, actor, action_date, detail) VALUES (?, ?, ?, ?, ?)",
        (bill_id, action, actor, date.today().isoformat(), detail),
    )
    if should_close:
        conn.commit()
        conn.close()


def check_daily_issue_limit(user_id: int, amount: float, conn) -> Optional[str]:
    cursor = conn.cursor()
    today = date.today().isoformat()
    cursor.execute(
        "SELECT COALESCE(SUM(amount), 0) as total FROM bills WHERE issuer_id = ? AND issue_date = ? AND status != '已作废'",
        (user_id, today),
    )
    today_total = cursor.fetchone()["total"]
    cursor.execute(
        "SELECT daily_issue_limit FROM credit_limits WHERE target_type = '经办人' AND target_id = ?",
        (user_id,),
    )
    limit_row = cursor.fetchone()
    if limit_row and limit_row["daily_issue_limit"] > 0:
        if today_total + amount > limit_row["daily_issue_limit"]:
            return f"单日签发额度超限（已签 {today_total} 两，额度 {limit_row['daily_issue_limit']} 两）"
    return None


def check_single_redeem_limit(user_id: int, amount: float, conn) -> Optional[str]:
    cursor = conn.cursor()
    cursor.execute(
        "SELECT single_redeem_limit FROM credit_limits WHERE target_type = '经办人' AND target_id = ?",
        (user_id,),
    )
    limit_row = cursor.fetchone()
    if limit_row and limit_row["single_redeem_limit"] > 0:
        if amount > limit_row["single_redeem_limit"]:
            return f"单笔兑付超限（申请 {amount} 两，上限 {limit_row['single_redeem_limit']} 两）"
    return None


def check_branch_limit(branch_id: int, amount: float, conn) -> Optional[str]:
    if not branch_id:
        return None
    cursor = conn.cursor()
    today = date.today().isoformat()
    cursor.execute(
        "SELECT COALESCE(SUM(amount), 0) as total FROM bills WHERE issue_branch_id = ? AND issue_date = ? AND status != '已作废'",
        (branch_id, today),
    )
    today_total = cursor.fetchone()["total"]
    cursor.execute(
        "SELECT daily_issue_limit, balance_warning FROM credit_limits WHERE target_type = '分号' AND target_id = ?",
        (branch_id,),
    )
    limit_row = cursor.fetchone()
    if limit_row:
        if limit_row["daily_issue_limit"] > 0 and today_total + amount > limit_row["daily_issue_limit"]:
            return f"分号单日签发额度超限（已签 {today_total} 两，额度 {limit_row['daily_issue_limit']} 两）"
    return None


def check_manager_issue_limit(user_id: int, branch_id: int, amount: float, conn) -> Optional[str]:
    if not branch_id:
        return None
    cursor = conn.cursor()
    today = date.today().isoformat()
    cursor.execute(
        "SELECT COALESCE(SUM(amount), 0) as total FROM bills WHERE issue_branch_id = ? AND issue_date = ? AND status != '已作废'",
        (branch_id, today),
    )
    today_total = cursor.fetchone()["total"]
    cursor.execute(
        "SELECT daily_issue_limit FROM credit_limits WHERE target_type = '掌柜' AND target_id = ?",
        (branch_id,),
    )
    limit_row = cursor.fetchone()
    if limit_row and limit_row["daily_issue_limit"] > 0:
        if today_total + amount > limit_row["daily_issue_limit"]:
            return f"掌柜管辖分号单日签发额度超限（已签 {today_total} 两，额度 {limit_row['daily_issue_limit']} 两）"
    return None


def check_manager_redeem_limit(branch_id: int, amount: float, conn) -> Optional[str]:
    if not branch_id:
        return None
    cursor = conn.cursor()
    cursor.execute(
        "SELECT single_redeem_limit FROM credit_limits WHERE target_type = '掌柜' AND target_id = ?",
        (branch_id,),
    )
    limit_row = cursor.fetchone()
    if limit_row and limit_row["single_redeem_limit"] > 0:
        if amount > limit_row["single_redeem_limit"]:
            return f"单笔兑付超过掌柜管辖上限（申请 {amount} 两，上限 {limit_row['single_redeem_limit']} 两）"
    return None


def auto_create_clearing(bill_id: int, conn):
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM bills WHERE id = ?", (bill_id,))
    bill = cursor.fetchone()
    if not bill:
        return
    cursor.execute(
        "SELECT * FROM redemptions WHERE bill_id = ? AND status = '已完成' ORDER BY id DESC LIMIT 1",
        (bill_id,),
    )
    redemption = cursor.fetchone()
    if not redemption:
        return
    from_branch_id = bill["issue_branch_id"]
    to_branch_id = redemption["redeem_branch_id"]
    if not from_branch_id or not to_branch_id or from_branch_id == to_branch_id:
        return
    today = date.today().isoformat()
    cursor.execute(
        "INSERT INTO clearings (from_branch_id, to_branch_id, bill_id, amount, clearing_date, clearing_type, status, operator_id, remark, created_at) VALUES (?, ?, ?, ?, ?, '兑付清算', '待清算', ?, ?, ?)",
        (from_branch_id, to_branch_id, bill_id, redemption["amount"], today, redemption["operator_id"], f"汇票 {bill['bill_no']} 兑付自动生成", today),
    )

def add_audit_log(business_type: str, business_id: int, action: str, user: dict, detail: str = "", conn=None):
    should_close = conn is None
    if conn is None:
        conn = get_db()
    cursor = conn.cursor()
    today = date.today().isoformat()
    cursor.execute(
        "INSERT INTO audit_logs (business_type, business_id, action, actor_id, actor_name, actor_role, detail, ip_address, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (business_type, business_id, action, user["id"], user["real_name"], user["role"], detail, "", today),
    )
    if should_close:
        conn.commit()
        conn.close()


def add_credit_occupation(customer_name: str, loan_id: int, amount: float, occupy_type: str, operator_id: int, remark: str = "", conn=None):
    should_close = conn is None
    if conn is None:
        conn = get_db()
    cursor = conn.cursor()
    today = date.today().isoformat()
    cursor.execute(
        "INSERT INTO credit_occupations (customer_name, loan_id, occupy_amount, occupy_type, occupy_date, operator_id, remark, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (customer_name, loan_id, amount, occupy_type, today, operator_id, remark, today),
    )
    if occupy_type == "占用":
        cursor.execute(
            "UPDATE customer_credits SET used_limit = used_limit + ? WHERE customer_name = ? AND status = '生效'",
            (amount, customer_name),
        )
    elif occupy_type == "释放":
        cursor.execute(
            "UPDATE customer_credits SET used_limit = CASE WHEN used_limit - ? < 0 THEN 0 ELSE used_limit - ? END WHERE customer_name = ? AND status = '生效'",
            (amount, amount, customer_name),
        )
    if should_close:
        conn.commit()
        conn.close()


def check_overdue_loans(conn):
    cursor = conn.cursor()
    today = date.today()
    today_str = today.isoformat()
    cursor.execute(
        "SELECT fl.*, b.bill_no FROM finance_loans fl LEFT JOIN bills b ON fl.bill_id = b.id WHERE fl.status IN ('已放款', '还款中') AND fl.due_date IS NOT NULL"
    )
    active_loans = [dict(row) for row in cursor.fetchall()]
    for loan in active_loans:
        due_date = loan["due_date"]
        days_before = (date.fromisoformat(due_date) - today).days
        if days_before <= 0:
            warning_type = "严重逾期"
            message = f"客户 {loan['customer_name']} 融资已逾期 {-days_before} 天，金额 {loan['loan_amount'] - loan['paid_amount']} 两"
        elif days_before <= 3:
            warning_type = "逾期预警"
            message = f"客户 {loan['customer_name']} 融资将于 {days_before} 天后到期，剩余 {loan['loan_amount'] - loan['paid_amount']} 两"
        elif days_before <= 7:
            warning_type = "到期预警"
            message = f"客户 {loan['customer_name']} 融资将于 {days_before} 天后到期，剩余 {loan['loan_amount'] - loan['paid_amount']} 两"
        else:
            continue
        cursor.execute(
            "SELECT id FROM overdue_warnings WHERE loan_id = ? AND warning_type = ? AND status = '待处理'",
            (loan["id"], warning_type),
        )
        if cursor.fetchone():
            continue
        cursor.execute(
            "INSERT INTO overdue_warnings (loan_id, warning_type, warning_date, days_before_due, message, status, created_at) VALUES (?, ?, ?, ?, ?, '待处理', ?)",
            (loan["id"], warning_type, today_str, days_before, message, today_str),
        )
    cursor.execute(
        "SELECT fl.*, b.bill_no FROM finance_loans fl LEFT JOIN bills b ON fl.bill_id = b.id WHERE fl.status = '已逾期'"
    )
    overdue_loans = [dict(row) for row in cursor.fetchall()]
    for loan in overdue_loans:
        if not loan["due_date"]:
            continue
        days_overdue = (today - date.fromisoformat(loan["due_date"])).days
        cursor.execute(
            "SELECT id FROM overdue_warnings WHERE loan_id = ? AND warning_type = '严重逾期' AND warning_date = ?",
            (loan["id"], today_str),
        )
        if cursor.fetchone():
            continue
        message = f"客户 {loan['customer_name']} 融资已逾期 {days_overdue} 天，剩余 {loan['loan_amount'] - loan['paid_amount']} 两"
        cursor.execute(
            "INSERT INTO overdue_warnings (loan_id, warning_type, warning_date, days_before_due, message, status, created_at) VALUES (?, ?, ?, ?, ?, '待处理', ?)",
            (loan["id"], "严重逾期", today_str, -days_overdue, message, today_str),
        )


def get_approval_chain(business_type: str, amount: float, conn):
    cursor = conn.cursor()
    cursor.execute(
        "SELECT * FROM approval_chains WHERE business_type = ? AND amount_threshold <= ? ORDER BY step_order",
        (business_type, amount),
    )
    return [dict(row) for row in cursor.fetchall()]


def get_next_approval_step(business_type: str, business_id: int, amount: float, conn):
    chain = get_approval_chain(business_type, amount, conn)
    if not chain:
        return None
    cursor = conn.cursor()
    cursor.execute(
        "SELECT step_order FROM approval_records WHERE business_type = ? AND business_id = ? AND action = '通过' ORDER BY step_order DESC LIMIT 1",
        (business_type, business_id),
    )
    last_approved = cursor.fetchone()
    if not last_approved:
        return chain[0]
    last_step = last_approved["step_order"]
    for step in chain:
        if step["step_order"] > last_step:
            return step
    return None


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    user = request.session.get("user")
    if user:
        return RedirectResponse("/dashboard", status_code=303)
    return RedirectResponse("/login", status_code=303)


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})


@app.post("/login")
def login(request: Request, username: str = Form(...)):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE username = ?", (username,))
    user = cursor.fetchone()
    conn.close()
    if not user:
        return templates.TemplateResponse(
            "login.html", {"request": request, "error": "用户名不存在"}
        )
    user_dict = {
        "id": user["id"],
        "username": user["username"],
        "real_name": user["real_name"],
        "role": user["role"],
        "branch_id": user["branch_id"],
    }
    request.session["user"] = user_dict
    return RedirectResponse("/dashboard", status_code=303)


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, user=Depends(auth_dep)):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) as cnt FROM bills")
    bill_count = cursor.fetchone()["cnt"]
    cursor.execute("SELECT COUNT(*) as cnt FROM bills WHERE status = '有效'")
    active_count = cursor.fetchone()["cnt"]
    cursor.execute("SELECT COUNT(*) as cnt FROM bills WHERE status = '已兑付'")
    redeemed_count = cursor.fetchone()["cnt"]
    cursor.execute("SELECT COUNT(*) as cnt FROM bills WHERE status = '已作废'")
    voided_count = cursor.fetchone()["cnt"]
    cursor.execute("SELECT COUNT(*) as cnt FROM bills WHERE status = '挂失'")
    lost_count = cursor.fetchone()["cnt"]
    cursor.execute("SELECT COUNT(*) as cnt FROM bills WHERE status = '冻结'")
    frozen_count = cursor.fetchone()["cnt"]
    cursor.execute("SELECT COUNT(*) as cnt FROM redemptions WHERE status = '待复核'")
    pending_review = cursor.fetchone()["cnt"]
    cursor.execute("SELECT COUNT(*) as cnt FROM clearings WHERE status = '待清算'")
    pending_clearing = cursor.fetchone()["cnt"]
    cursor.execute("SELECT COUNT(*) as cnt FROM finance_loans WHERE status = '待审核'")
    pending_finance = cursor.fetchone()["cnt"]
    cursor.execute("SELECT COUNT(*) as cnt FROM finance_loans WHERE status = '已逾期'")
    overdue_finance = cursor.fetchone()["cnt"]
    cursor.execute("SELECT COUNT(*) as cnt FROM finance_loans WHERE status IN ('已放款', '还款中', '已逾期')")
    active_finance = cursor.fetchone()["cnt"]
    cursor.execute(
        "SELECT b.*, u.real_name as issuer_name, br.branch_name as issue_branch_name FROM bills b LEFT JOIN users u ON b.issuer_id = u.id LEFT JOIN branches br ON b.issue_branch_id = br.id ORDER BY b.id DESC LIMIT 10"
    )
    recent_bills = [dict(row) for row in cursor.fetchall()]

    warnings = []
    today = date.today().isoformat()
    cursor.execute(
        "SELECT cl.target_type, cl.target_id, cl.daily_issue_limit, u.real_name, b.branch_name FROM credit_limits cl LEFT JOIN users u ON cl.target_type = '经办人' AND cl.target_id = u.id LEFT JOIN branches b ON cl.target_type = '分号' AND cl.target_id = b.id WHERE cl.daily_issue_limit > 0"
    )
    for row in cursor.fetchall():
        if row["target_type"] == "经办人":
            cursor.execute(
                "SELECT COALESCE(SUM(amount), 0) as total FROM bills WHERE issuer_id = ? AND issue_date = ? AND status != '已作废'",
                (row["target_id"], today),
            )
            total = cursor.fetchone()["total"]
            if total > row["daily_issue_limit"] * 0.8:
                warnings.append(f"经办人 {row['real_name']} 今日已签发 {total} 两，达额度 {row['daily_issue_limit']} 两的 {int(total/row['daily_issue_limit']*100)}%")
        elif row["target_type"] == "分号":
            cursor.execute(
                "SELECT COALESCE(SUM(amount), 0) as total FROM bills WHERE issue_branch_id = ? AND issue_date = ? AND status != '已作废'",
                (row["target_id"], today),
            )
            total = cursor.fetchone()["total"]
            if total > row["daily_issue_limit"] * 0.8:
                warnings.append(f"分号 {row['branch_name']} 今日已签发 {total} 两，达额度 {row['daily_issue_limit']} 两的 {int(total/row['daily_issue_limit']*100)}%")

    check_overdue_loans(conn)
    cursor.execute("SELECT COUNT(*) as cnt FROM overdue_warnings WHERE status = '待处理'")
    overdue_warning_count = cursor.fetchone()["cnt"]
    if overdue_warning_count > 0:
        warnings.append(f"有 {overdue_warning_count} 条逾期预警待处理")
    conn.close()
    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "user": user,
            "bill_count": bill_count,
            "active_count": active_count,
            "redeemed_count": redeemed_count,
            "voided_count": voided_count,
            "lost_count": lost_count,
            "frozen_count": frozen_count,
            "pending_review": pending_review,
            "pending_clearing": pending_clearing,
            "pending_finance": pending_finance,
            "overdue_finance": overdue_finance,
            "active_finance": active_finance,
            "recent_bills": recent_bills,
            "warnings": warnings,
        },
    )


@app.get("/bills", response_class=HTMLResponse)
def bills_list(request: Request, user=Depends(auth_dep), status: Optional[str] = None):
    conn = get_db()
    cursor = conn.cursor()
    if status and status in ("有效", "已兑付", "已作废", "挂失", "冻结"):
        cursor.execute(
            "SELECT b.*, u.real_name as issuer_name, br.branch_name as issue_branch_name FROM bills b LEFT JOIN users u ON b.issuer_id = u.id LEFT JOIN branches br ON b.issue_branch_id = br.id WHERE b.status = ? ORDER BY b.id DESC",
            (status,),
        )
    else:
        cursor.execute(
            "SELECT b.*, u.real_name as issuer_name, br.branch_name as issue_branch_name FROM bills b LEFT JOIN users u ON b.issuer_id = u.id LEFT JOIN branches br ON b.issue_branch_id = br.id ORDER BY b.id DESC"
        )
    bills = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return templates.TemplateResponse(
        "bills.html",
        {"request": request, "user": user, "bills": bills, "current_status": status},
    )


@app.get("/bills/create", response_class=HTMLResponse)
def bill_create_page(request: Request, user=Depends(auth_dep)):
    if user["role"] not in OPERATOR_ROLES:
        raise HTTPException(status_code=403, detail="无权签发汇票")
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM branches WHERE status = '营业中' ORDER BY id")
    branches = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return templates.TemplateResponse(
        "bill_create.html", {"request": request, "user": user, "branches": branches}
    )


@app.post("/bills/create")
def bill_create(
    request: Request,
    bill_no: str = Form(...),
    amount: float = Form(...),
    payee: str = Form(...),
    issue_date: str = Form(...),
    due_date: str = Form(default=""),
    issue_branch_id: str = Form(default=""),
    remark: str = Form(default=""),
    user=Depends(auth_dep),
):
    if user["role"] not in OPERATOR_ROLES:
        raise HTTPException(status_code=403, detail="无权签发汇票")

    errors = []
    if amount <= 0:
        errors.append("票面金额必须大于零")

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM bills WHERE bill_no = ?", (bill_no,))
    if cursor.fetchone():
        errors.append("票号已存在，必须唯一")

    branch_id = int(issue_branch_id) if issue_branch_id else None
    if not branch_id:
        branch_id = user.get("branch_id")

    limit_err = check_daily_issue_limit(user["id"], amount, conn)
    if limit_err:
        errors.append(limit_err)

    if branch_id:
        branch_err = check_branch_limit(branch_id, amount, conn)
        if branch_err:
            errors.append(branch_err)
        manager_err = check_manager_issue_limit(user["id"], branch_id, amount, conn)
        if manager_err:
            errors.append(manager_err)

    if errors:
        cursor.execute("SELECT * FROM branches WHERE status = '营业中' ORDER BY id")
        branches = [dict(row) for row in cursor.fetchall()]
        conn.close()
        return templates.TemplateResponse(
            "bill_create.html",
            {"request": request, "user": user, "errors": errors, "form": {
                "bill_no": bill_no, "amount": amount, "payee": payee,
                "issue_date": issue_date, "due_date": due_date, "issue_branch_id": issue_branch_id, "remark": remark
            }, "branches": branches},
        )

    try:
        cursor.execute(
            "INSERT INTO bills (bill_no, amount, issuer_id, issue_branch_id, payee, issue_date, due_date, status, remark) VALUES (?, ?, ?, ?, ?, ?, ?, '有效', ?)",
            (bill_no, amount, user["id"], branch_id, payee, issue_date, due_date or None, remark),
        )
        bill_id = cursor.lastrowid
        add_timeline(
            bill_id,
            "签发",
            user["real_name"],
            f"签发汇票 {bill_no}，票面金额 {amount} 两，收款人 {payee}",
            conn,
        )
        conn.commit()
        conn.close()
        return RedirectResponse(f"/bills/{bill_id}", status_code=303)
    except Exception as e:
        conn.close()
        cursor.execute("SELECT * FROM branches WHERE status = '营业中' ORDER BY id")
        branches = [dict(row) for row in cursor.fetchall()]
        return templates.TemplateResponse(
            "bill_create.html",
            {"request": request, "user": user, "errors": [str(e)], "branches": branches},
        )


@app.get("/bills/batch-create", response_class=HTMLResponse)
def batch_create_page(request: Request, user=Depends(auth_dep)):
    if user["role"] not in OPERATOR_ROLES:
        raise HTTPException(status_code=403, detail="无权签发汇票")
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM branches WHERE status = '营业中' ORDER BY id")
    branches = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return templates.TemplateResponse(
        "batch_create.html", {"request": request, "user": user, "branches": branches}
    )


@app.post("/bills/batch-create")
def batch_create_submit(
    request: Request,
    bills_data: str = Form(...),
    issue_branch_id: str = Form(default=""),
    user=Depends(auth_dep),
):
    if user["role"] not in OPERATOR_ROLES:
        raise HTTPException(status_code=403, detail="无权签发汇票")

    conn = get_db()
    cursor = conn.cursor()
    branch_id = int(issue_branch_id) if issue_branch_id else user.get("branch_id")

    try:
        items = json.loads(bills_data)
    except json.JSONDecodeError:
        conn.close()
        return templates.TemplateResponse(
            "batch_create.html",
            {"request": request, "user": user, "error": "数据格式错误，请检查JSON格式"},
        )

    results = []
    errors = []
    for i, item in enumerate(items):
        bill_no = item.get("bill_no", "")
        amount = float(item.get("amount", 0))
        payee = item.get("payee", "")
        issue_date = item.get("issue_date", date.today().isoformat())
        due_date = item.get("due_date", "")

        item_errors = []
        if not bill_no:
            item_errors.append("票号为空")
        if amount <= 0:
            item_errors.append("金额必须大于零")
        if not payee:
            item_errors.append("收款人为空")

        cursor.execute("SELECT id FROM bills WHERE bill_no = ?", (bill_no,))
        if cursor.fetchone():
            item_errors.append("票号已存在")

        limit_err = check_daily_issue_limit(user["id"], amount, conn)
        if limit_err:
            item_errors.append(limit_err)

        if branch_id:
            branch_err = check_branch_limit(branch_id, amount, conn)
            if branch_err:
                item_errors.append(branch_err)
            manager_err = check_manager_issue_limit(user["id"], branch_id, amount, conn)
            if manager_err:
                item_errors.append(manager_err)

        if item_errors:
            errors.append(f"第{i+1}条：{'，'.join(item_errors)}")
            continue

        try:
            cursor.execute(
                "INSERT INTO bills (bill_no, amount, issuer_id, issue_branch_id, payee, issue_date, due_date, status, remark) VALUES (?, ?, ?, ?, ?, ?, ?, '有效', '')",
                (bill_no, amount, user["id"], branch_id, payee, issue_date, due_date or None),
            )
            bill_id = cursor.lastrowid
            add_timeline(bill_id, "签发", user["real_name"], f"批量签发汇票 {bill_no}，金额 {amount} 两", conn)
            results.append(f"第{i+1}条：{bill_no} 签发成功")
        except Exception as e:
            errors.append(f"第{i+1}条：{str(e)}")

    conn.commit()
    conn.close()
    return templates.TemplateResponse(
        "batch_result.html",
        {"request": request, "user": user, "results": results, "errors": errors, "action": "批量签发"},
    )


@app.get("/bills/{bill_id}", response_class=HTMLResponse)
def bill_detail(request: Request, bill_id: int, user=Depends(auth_dep)):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT b.*, u.real_name as issuer_name, br.branch_name as issue_branch_name FROM bills b LEFT JOIN users u ON b.issuer_id = u.id LEFT JOIN branches br ON b.issue_branch_id = br.id WHERE b.id = ?",
        (bill_id,),
    )
    bill = cursor.fetchone()
    if not bill:
        conn.close()
        raise HTTPException(status_code=404, detail="汇票不存在")

    bill_dict = dict(bill)
    cursor.execute(
        "SELECT e.*, u.real_name as operator_name FROM endorsements e LEFT JOIN users u ON e.operator_id = u.id WHERE e.bill_id = ? ORDER BY e.id",
        (bill_id,),
    )
    endorsements = [dict(row) for row in cursor.fetchall()]

    cursor.execute(
        "SELECT r.*, u.real_name as operator_name, rv.real_name as reviewer_name, br.branch_name as redeem_branch_name FROM redemptions r LEFT JOIN users u ON r.operator_id = u.id LEFT JOIN users rv ON r.reviewer_id = rv.id LEFT JOIN branches br ON r.redeem_branch_id = br.id WHERE r.bill_id = ? ORDER BY r.id",
        (bill_id,),
    )
    redemptions = [dict(row) for row in cursor.fetchall()]

    cursor.execute(
        "SELECT * FROM timeline WHERE bill_id = ? ORDER BY id", (bill_id,)
    )
    timeline = [dict(row) for row in cursor.fetchall()]

    cursor.execute(
        "SELECT er.*, u.real_name as operator_name FROM exception_records er LEFT JOIN users u ON er.operator_id = u.id WHERE er.bill_id = ? ORDER BY er.id",
        (bill_id,),
    )
    exceptions = [dict(row) for row in cursor.fetchall()]

    cursor.execute(
        "SELECT c.*, bf.branch_name as from_branch_name, bt.branch_name as to_branch_name, u.real_name as operator_name FROM clearings c LEFT JOIN branches bf ON c.from_branch_id = bf.id LEFT JOIN branches bt ON c.to_branch_id = bt.id LEFT JOIN users u ON c.operator_id = u.id WHERE c.bill_id = ? ORDER BY c.id",
        (bill_id,),
    )
    clearings = [dict(row) for row in cursor.fetchall()]

    current_payee = get_current_payee(bill_id, conn)

    cursor.execute(
        "SELECT fl.*, u1.real_name as applicant_name, u2.real_name as reviewer_name, u3.real_name as approver_name FROM finance_loans fl LEFT JOIN users u1 ON fl.applicant_id = u1.id LEFT JOIN users u2 ON fl.reviewer_id = u2.id LEFT JOIN users u3 ON fl.approver_id = u3.id WHERE fl.bill_id = ? ORDER BY fl.id DESC",
        (bill_id,),
    )
    finance_loans = [dict(row) for row in cursor.fetchall()]

    conn.close()

    bill_dict["endorsements"] = endorsements
    bill_dict["redemptions"] = redemptions

    can_endorse = bill_dict["status"] == "有效" and user["role"] in OPERATOR_ROLES
    can_redeem = bill_dict["status"] == "有效" and user["role"] in OPERATOR_ROLES
    can_void = bill_dict["status"] == "有效" and user["role"] in MANAGER_ROLES
    can_lost = bill_dict["status"] == "有效" and user["role"] in MANAGER_ROLES
    can_freeze = bill_dict["status"] == "有效" and user["role"] in MANAGER_ROLES
    can_unfreeze = bill_dict["status"] == "冻结" and user["role"] in MANAGER_ROLES
    can_recover = bill_dict["status"] in ("挂失", "冻结") and user["role"] in MANAGER_ROLES
    can_reverse = bill_dict["status"] == "已兑付" and user["role"] == "总号掌柜"

    return templates.TemplateResponse(
        "bill_detail.html",
        {
            "request": request,
            "user": user,
            "bill": bill_dict,
            "endorsements": endorsements,
            "redemptions": redemptions,
            "timeline": timeline,
            "exceptions": exceptions,
            "clearings": clearings,
            "current_payee": current_payee,
            "finance_loans": finance_loans,
            "can_endorse": can_endorse,
            "can_redeem": can_redeem,
            "can_void": can_void,
            "can_lost": can_lost,
            "can_freeze": can_freeze,
            "can_unfreeze": can_unfreeze,
            "can_recover": can_recover,
            "can_reverse": can_reverse,
            "high_value_threshold": HIGH_VALUE_THRESHOLD,
        },
    )


@app.post("/bills/{bill_id}/void")
def bill_void(request: Request, bill_id: int, user=Depends(auth_dep)):
    if user["role"] not in MANAGER_ROLES:
        raise HTTPException(status_code=403, detail="仅掌柜可作废汇票")

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM bills WHERE id = ?", (bill_id,))
    bill = cursor.fetchone()
    if not bill:
        conn.close()
        raise HTTPException(status_code=404, detail="汇票不存在")
    if bill["status"] not in ("有效",):
        conn.close()
        raise HTTPException(status_code=400, detail="仅有效汇票可作废")

    cursor.execute("UPDATE bills SET status = '已作废' WHERE id = ?", (bill_id,))
    add_timeline(bill_id, "作废", user["real_name"], f"汇票 {bill['bill_no']} 已作废", conn)
    conn.commit()
    conn.close()
    return RedirectResponse(f"/bills/{bill_id}", status_code=303)


@app.get("/bills/{bill_id}/endorse", response_class=HTMLResponse)
def endorse_page(request: Request, bill_id: int, user=Depends(auth_dep)):
    if user["role"] not in OPERATOR_ROLES:
        raise HTTPException(status_code=403, detail="无权登记背书")

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM bills WHERE id = ?", (bill_id,))
    bill = cursor.fetchone()

    if not bill:
        conn.close()
        raise HTTPException(status_code=404, detail="汇票不存在")
    if bill["status"] != "有效":
        conn.close()
        raise HTTPException(status_code=400, detail="该汇票不可背书")

    current_payee = get_current_payee(bill_id, conn)
    conn.close()
    return templates.TemplateResponse(
        "endorse.html",
        {
            "request": request,
            "user": user,
            "bill": dict(bill),
            "current_payee": current_payee,
        },
    )


@app.post("/bills/{bill_id}/endorse")
def endorse_submit(
    request: Request,
    bill_id: int,
    endorser: str = Form(...),
    endorsee: str = Form(...),
    endorse_date: str = Form(...),
    user=Depends(auth_dep),
):
    if user["role"] not in OPERATOR_ROLES:
        raise HTTPException(status_code=403, detail="无权登记背书")

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM bills WHERE id = ?", (bill_id,))
    bill = cursor.fetchone()

    if not bill:
        conn.close()
        raise HTTPException(status_code=404, detail="汇票不存在")

    errors = []
    if bill["status"] != "有效":
        errors.append("该汇票不可背书（已作废或已兑付或已挂失或已冻结）")

    if endorse_date <= bill["issue_date"]:
        errors.append(f"背书日期必须晚于签发日期（{bill['issue_date']}）")

    current_payee = get_current_payee(bill_id, conn)
    if endorser != current_payee:
        errors.append(f"背书人必须是当前持票人（{current_payee}）")

    if errors:
        conn.close()
        return templates.TemplateResponse(
            "endorse.html",
            {
                "request": request,
                "user": user,
                "bill": dict(bill),
                "current_payee": current_payee,
                "errors": errors,
                "form": {"endorser": endorser, "endorsee": endorsee, "endorse_date": endorse_date},
            },
        )

    cursor.execute(
        "INSERT INTO endorsements (bill_id, endorser, endorsee, endorse_date, operator_id) VALUES (?, ?, ?, ?, ?)",
        (bill_id, endorser, endorsee, endorse_date, user["id"]),
    )
    add_timeline(
        bill_id,
        "背书",
        user["real_name"],
        f"{endorser} → {endorsee}，背书日期 {endorse_date}",
        conn,
    )
    conn.commit()
    conn.close()
    return RedirectResponse(f"/bills/{bill_id}", status_code=303)


@app.get("/bills/{bill_id}/redeem", response_class=HTMLResponse)
def redeem_page(request: Request, bill_id: int, user=Depends(auth_dep)):
    if user["role"] not in OPERATOR_ROLES:
        raise HTTPException(status_code=403, detail="无权提交兑付")

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM bills WHERE id = ?", (bill_id,))
    bill = cursor.fetchone()

    if not bill:
        conn.close()
        raise HTTPException(status_code=404, detail="汇票不存在")
    if bill["status"] != "有效":
        conn.close()
        raise HTTPException(status_code=400, detail="该汇票不可兑付")

    current_payee = get_current_payee(bill_id, conn)
    cursor.execute("SELECT * FROM branches WHERE status = '营业中' ORDER BY id")
    branches = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return templates.TemplateResponse(
        "redeem.html",
        {
            "request": request,
            "user": user,
            "bill": dict(bill),
            "current_payee": current_payee,
            "branches": branches,
        },
    )


@app.post("/bills/{bill_id}/redeem")
def redeem_submit(
    request: Request,
    bill_id: int,
    amount: float = Form(...),
    request_date: str = Form(...),
    redeem_branch_id: str = Form(default=""),
    user=Depends(auth_dep),
):
    if user["role"] not in OPERATOR_ROLES:
        raise HTTPException(status_code=403, detail="无权提交兑付")

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM bills WHERE id = ?", (bill_id,))
    bill = cursor.fetchone()

    if not bill:
        conn.close()
        raise HTTPException(status_code=404, detail="汇票不存在")

    current_payee = get_current_payee(bill_id, conn)
    r_branch_id = int(redeem_branch_id) if redeem_branch_id else user.get("branch_id")

    errors = []
    if bill["status"] != "有效":
        errors.append("该汇票不可兑付")

    if amount <= 0:
        errors.append("兑付金额必须大于零")

    if amount > bill["amount"]:
        errors.append(f"兑付金额不能超过票面金额（{bill['amount']} 两）")

    limit_err = check_single_redeem_limit(user["id"], amount, conn)
    if limit_err:
        errors.append(limit_err)

    if r_branch_id:
        mgr_redeem_err = check_manager_redeem_limit(r_branch_id, amount, conn)
        if mgr_redeem_err:
            errors.append(mgr_redeem_err)

    if errors:
        cursor.execute("SELECT * FROM branches WHERE status = '营业中' ORDER BY id")
        branches = [dict(row) for row in cursor.fetchall()]
        conn.close()
        return templates.TemplateResponse(
            "redeem.html",
            {
                "request": request,
                "user": user,
                "bill": dict(bill),
                "current_payee": current_payee,
                "branches": branches,
                "errors": errors,
                "form": {"amount": amount, "request_date": request_date, "redeem_branch_id": redeem_branch_id},
            },
        )

    is_high_value = bill["amount"] >= HIGH_VALUE_THRESHOLD

    if not is_high_value:
        cursor.execute(
            "INSERT INTO redemptions (bill_id, payee, amount, request_date, operator_id, status, reviewer_id, review_date, redeem_branch_id) VALUES (?, ?, ?, ?, ?, '已完成', NULL, ?, ?)",
            (bill_id, current_payee, amount, request_date, user["id"], date.today().isoformat(), r_branch_id),
        )
        cursor.execute("UPDATE bills SET status = '已兑付' WHERE id = ?", (bill_id,))
        add_timeline(
            bill_id,
            "兑付",
            user["real_name"],
            f"兑付完成，金额 {amount} 两，收款人 {current_payee}",
            conn,
        )
        auto_create_clearing(bill_id, conn)
    else:
        cursor.execute(
            "INSERT INTO redemptions (bill_id, payee, amount, request_date, operator_id, status, redeem_branch_id) VALUES (?, ?, ?, ?, ?, '待复核', ?)",
            (bill_id, current_payee, amount, request_date, user["id"], r_branch_id),
        )
        add_timeline(
            bill_id,
            "兑付申请",
            user["real_name"],
            f"提交兑付申请，金额 {amount} 两，收款人 {current_payee}（高额汇票，待复核）",
            conn,
        )

    conn.commit()
    conn.close()
    return RedirectResponse(f"/bills/{bill_id}", status_code=303)


@app.get("/redemptions", response_class=HTMLResponse)
def redemptions_list(request: Request, user=Depends(auth_dep)):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT r.*, b.bill_no, b.amount as bill_amount, u.real_name as operator_name, br.branch_name as redeem_branch_name FROM redemptions r LEFT JOIN bills b ON r.bill_id = b.id LEFT JOIN users u ON r.operator_id = u.id LEFT JOIN branches br ON r.redeem_branch_id = br.id ORDER BY r.id DESC"
    )
    redemptions = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return templates.TemplateResponse(
        "redemptions.html",
        {"request": request, "user": user, "redemptions": redemptions},
    )


@app.get("/redemptions/batch", response_class=HTMLResponse)
def batch_redeem_page(request: Request, user=Depends(auth_dep)):
    if user["role"] not in OPERATOR_ROLES:
        raise HTTPException(status_code=403, detail="无权批量兑付")
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM branches WHERE status = '营业中' ORDER BY id")
    branches = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return templates.TemplateResponse(
        "batch_redeem.html", {"request": request, "user": user, "branches": branches}
    )


@app.post("/redemptions/batch")
def batch_redeem_submit(
    request: Request,
    bills_data: str = Form(...),
    redeem_branch_id: str = Form(default=""),
    user=Depends(auth_dep),
):
    if user["role"] not in OPERATOR_ROLES:
        raise HTTPException(status_code=403, detail="无权批量兑付")

    conn = get_db()
    cursor = conn.cursor()
    r_branch_id = int(redeem_branch_id) if redeem_branch_id else user.get("branch_id")

    try:
        items = json.loads(bills_data)
    except json.JSONDecodeError:
        conn.close()
        return templates.TemplateResponse(
            "batch_redeem.html",
            {"request": request, "user": user, "error": "数据格式错误"},
        )

    results = []
    errors = []
    for i, item in enumerate(items):
        bill_no = item.get("bill_no", "")
        amount = float(item.get("amount", 0))
        request_date = item.get("request_date", date.today().isoformat())

        cursor.execute("SELECT * FROM bills WHERE bill_no = ?", (bill_no,))
        bill = cursor.fetchone()

        item_errors = []
        if not bill:
            item_errors.append("汇票不存在")
        elif bill["status"] != "有效":
            item_errors.append(f"汇票状态为{bill['status']}，不可兑付")
        if amount <= 0:
            item_errors.append("金额必须大于零")
        if bill and amount > bill["amount"]:
            item_errors.append(f"超票面金额 {bill['amount']}")

        limit_err = check_single_redeem_limit(user["id"], amount, conn)
        if limit_err:
            item_errors.append(limit_err)

        if r_branch_id:
            mgr_redeem_err = check_manager_redeem_limit(r_branch_id, amount, conn)
            if mgr_redeem_err:
                item_errors.append(mgr_redeem_err)

        if item_errors:
            errors.append(f"第{i+1}条（{bill_no}）：{'，'.join(item_errors)}")
            continue

        current_payee = get_current_payee(bill["id"], conn)
        is_high_value = bill["amount"] >= HIGH_VALUE_THRESHOLD

        if not is_high_value:
            cursor.execute(
                "INSERT INTO redemptions (bill_id, payee, amount, request_date, operator_id, status, reviewer_id, review_date, redeem_branch_id) VALUES (?, ?, ?, ?, ?, '已完成', NULL, ?, ?)",
                (bill["id"], current_payee, amount, request_date, user["id"], date.today().isoformat(), r_branch_id),
            )
            cursor.execute("UPDATE bills SET status = '已兑付' WHERE id = ?", (bill["id"],))
            add_timeline(bill["id"], "兑付", user["real_name"], f"批量兑付完成，金额 {amount} 两", conn)
            auto_create_clearing(bill["id"], conn)
            results.append(f"第{i+1}条（{bill_no}）：兑付成功")
        else:
            cursor.execute(
                "INSERT INTO redemptions (bill_id, payee, amount, request_date, operator_id, status, redeem_branch_id) VALUES (?, ?, ?, ?, ?, '待复核', ?)",
                (bill["id"], current_payee, amount, request_date, user["id"], r_branch_id),
            )
            add_timeline(bill["id"], "兑付申请", user["real_name"], f"批量兑付申请，金额 {amount} 两（待复核）", conn)
            results.append(f"第{i+1}条（{bill_no}）：已提交待复核")

    conn.commit()
    conn.close()
    return templates.TemplateResponse(
        "batch_result.html",
        {"request": request, "user": user, "results": results, "errors": errors, "action": "批量兑付"},
    )


@app.get("/redemptions/batch-review", response_class=HTMLResponse)
def batch_review_page(request: Request, user=Depends(auth_dep)):
    if user["role"] not in REVIEWER_ROLES:
        raise HTTPException(status_code=403, detail="无权批量复核")
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT r.*, b.bill_no, b.amount as bill_amount, u.real_name as operator_name, br.branch_name as redeem_branch_name FROM redemptions r LEFT JOIN bills b ON r.bill_id = b.id LEFT JOIN users u ON r.operator_id = u.id LEFT JOIN branches br ON r.redeem_branch_id = br.id WHERE r.status = '待复核' ORDER BY r.id"
    )
    pending = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return templates.TemplateResponse(
        "batch_review.html", {"request": request, "user": user, "pending": pending}
    )


@app.post("/redemptions/batch-review")
def batch_review_submit(
    request: Request,
    review_data: str = Form(...),
    user=Depends(auth_dep),
):
    if user["role"] not in REVIEWER_ROLES:
        raise HTTPException(status_code=403, detail="无权批量复核")

    conn = get_db()
    cursor = conn.cursor()

    try:
        items = json.loads(review_data)
    except json.JSONDecodeError:
        conn.close()
        raise HTTPException(status_code=400, detail="数据格式错误")

    results = []
    errors = []
    for item in items:
        rid = int(item.get("id", 0))
        action = item.get("action", "")
        comment = item.get("comment", "")

        cursor.execute(
            "SELECT r.*, b.bill_no FROM redemptions r LEFT JOIN bills b ON r.bill_id = b.id WHERE r.id = ?",
            (rid,),
        )
        redemption = cursor.fetchone()
        if not redemption or redemption["status"] != "待复核":
            errors.append(f"兑付记录 {rid} 不可复核")
            continue

        if action == "approve":
            cursor.execute(
                "UPDATE redemptions SET status = '已完成', reviewer_id = ?, review_date = ?, review_comment = ? WHERE id = ?",
                (user["id"], date.today().isoformat(), comment, rid),
            )
            cursor.execute(
                "UPDATE bills SET status = '已兑付' WHERE id = ?", (redemption["bill_id"],)
            )
            add_timeline(
                redemption["bill_id"],
                "复核通过",
                user["real_name"],
                f"批量复核通过，金额 {redemption['amount']} 两" + (f"，意见：{comment}" if comment else ""),
                conn,
            )
            auto_create_clearing(redemption["bill_id"], conn)
            results.append(f"{redemption['bill_no']}：复核通过")
        elif action == "reject":
            cursor.execute(
                "UPDATE redemptions SET status = '已拒绝', reviewer_id = ?, review_date = ?, review_comment = ? WHERE id = ?",
                (user["id"], date.today().isoformat(), comment, rid),
            )
            add_timeline(
                redemption["bill_id"],
                "复核拒绝",
                user["real_name"],
                f"批量复核拒绝，汇票 {redemption['bill_no']}" + (f"，原因：{comment}" if comment else ""),
                conn,
            )
            results.append(f"{redemption['bill_no']}：已拒绝")

    conn.commit()
    conn.close()
    return templates.TemplateResponse(
        "batch_result.html",
        {"request": request, "user": user, "results": results, "errors": errors, "action": "批量复核"},
    )


@app.get("/redemptions/{redemption_id}/review", response_class=HTMLResponse)
def review_page(request: Request, redemption_id: int, user=Depends(auth_dep)):
    if user["role"] not in REVIEWER_ROLES:
        raise HTTPException(status_code=403, detail="仅复核人或掌柜可审核")

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT r.*, b.bill_no, b.amount as bill_amount, b.issue_date, u.real_name as operator_name, br.branch_name as redeem_branch_name FROM redemptions r LEFT JOIN bills b ON r.bill_id = b.id LEFT JOIN users u ON r.operator_id = u.id LEFT JOIN branches br ON r.redeem_branch_id = br.id WHERE r.id = ?",
        (redemption_id,),
    )
    redemption = cursor.fetchone()
    conn.close()

    if not redemption:
        raise HTTPException(status_code=404, detail="兑付记录不存在")
    if redemption["status"] != "待复核":
        raise HTTPException(status_code=400, detail="该兑付申请已处理")

    return templates.TemplateResponse(
        "redemption_review.html",
        {"request": request, "user": user, "redemption": dict(redemption)},
    )


@app.post("/redemptions/{redemption_id}/review")
def review_submit(
    request: Request,
    redemption_id: int,
    action: str = Form(...),
    review_comment: str = Form(default=""),
    user=Depends(auth_dep),
):
    if user["role"] not in REVIEWER_ROLES:
        raise HTTPException(status_code=403, detail="仅复核人或掌柜可审核")

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT r.*, b.bill_no FROM redemptions r LEFT JOIN bills b ON r.bill_id = b.id WHERE r.id = ?",
        (redemption_id,),
    )
    redemption = cursor.fetchone()

    if not redemption:
        conn.close()
        raise HTTPException(status_code=404, detail="兑付记录不存在")
    if redemption["status"] != "待复核":
        conn.close()
        raise HTTPException(status_code=400, detail="该兑付申请已处理")

    if action == "approve":
        cursor.execute(
            "UPDATE redemptions SET status = '已完成', reviewer_id = ?, review_date = ?, review_comment = ? WHERE id = ?",
            (user["id"], date.today().isoformat(), review_comment, redemption_id),
        )
        cursor.execute(
            "UPDATE bills SET status = '已兑付' WHERE id = ?", (redemption["bill_id"],)
        )
        add_timeline(
            redemption["bill_id"],
            "复核通过",
            user["real_name"],
            f"复核通过，兑付完成，金额 {redemption['amount']} 两，收款人 {redemption['payee']}"
            + (f"，复核意见：{review_comment}" if review_comment else ""),
            conn,
        )
        auto_create_clearing(redemption["bill_id"], conn)
    else:
        cursor.execute(
            "UPDATE redemptions SET status = '已拒绝', reviewer_id = ?, review_date = ?, review_comment = ? WHERE id = ?",
            (user["id"], date.today().isoformat(), review_comment, redemption_id),
        )
        add_timeline(
            redemption["bill_id"],
            "复核拒绝",
            user["real_name"],
            f"复核拒绝，汇票 {redemption['bill_no']} 兑付被驳回"
            + (f"，原因：{review_comment}" if review_comment else ""),
            conn,
        )

    conn.commit()
    conn.close()
    return RedirectResponse("/redemptions", status_code=303)


@app.get("/clearings", response_class=HTMLResponse)
def clearings_list(request: Request, type: str = "", user=Depends(auth_dep)):
    conn = get_db()
    cursor = conn.cursor()
    sql = "SELECT c.*, bf.branch_name as from_branch_name, bt.branch_name as to_branch_name, b.bill_no, u.real_name as operator_name FROM clearings c LEFT JOIN branches bf ON c.from_branch_id = bf.id LEFT JOIN branches bt ON c.to_branch_id = bt.id LEFT JOIN bills b ON c.bill_id = b.id LEFT JOIN users u ON c.operator_id = u.id"
    params = []
    if type:
        sql += " WHERE c.clearing_type = ?"
        params.append(type)
    sql += " ORDER BY c.id DESC"
    cursor.execute(sql, params)
    clearings = [dict(row) for row in cursor.fetchall()]
    pending_clearing = sum(1 for c in clearings if c["status"] == "待清算")

    cursor.execute(
        "SELECT cl.*, b.branch_name as display_name FROM credit_limits cl LEFT JOIN branches b ON cl.target_id = b.id AND cl.target_type = '分号' ORDER BY cl.target_type, cl.id"
    )
    limits = [dict(row) for row in cursor.fetchall()]
    warnings = []
    today = date.today().isoformat()
    for l in limits:
        if l["target_type"] == "分号" and l["balance_warning"] > 0:
            cursor.execute(
                "SELECT COALESCE(SUM(amount), 0) as total FROM bills WHERE issue_branch_id = ? AND issue_date = ? AND status != '已作废'",
                (l["target_id"], today),
            )
            today_total = cursor.fetchone()["total"]
            if today_total >= l["balance_warning"]:
                warnings.append(f"{l['display_name']}今日已签发 {today_total} 两，达到预警阈值 {l['balance_warning']} 两")
    conn.close()
    return templates.TemplateResponse(
        "clearings.html",
        {"request": request, "user": user, "clearings": clearings, "current_type": type, "pending_clearing": pending_clearing, "warnings": warnings},
    )


@app.get("/clearings/create", response_class=HTMLResponse)
def clearing_create_page(request: Request, user=Depends(auth_dep)):
    if user["role"] not in MANAGER_ROLES:
        raise HTTPException(status_code=403, detail="仅掌柜可登记清算")
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM branches WHERE status = '营业中' ORDER BY id")
    branches = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return templates.TemplateResponse(
        "clearing_create.html", {"request": request, "user": user, "branches": branches}
    )


@app.post("/clearings/create")
def clearing_create_submit(
    request: Request,
    from_branch_id: int = Form(...),
    to_branch_id: int = Form(...),
    amount: float = Form(...),
    clearing_date: str = Form(...),
    remark: str = Form(default=""),
    user=Depends(auth_dep),
):
    if user["role"] not in MANAGER_ROLES:
        raise HTTPException(status_code=403, detail="仅掌柜可登记清算")

    conn = get_db()
    cursor = conn.cursor()
    errors = []
    if from_branch_id == to_branch_id:
        errors.append("往来分号不能相同")
    if amount <= 0:
        errors.append("金额必须大于零")

    if errors:
        cursor.execute("SELECT * FROM branches WHERE status = '营业中' ORDER BY id")
        branches = [dict(row) for row in cursor.fetchall()]
        conn.close()
        return templates.TemplateResponse(
            "clearing_create.html",
            {"request": request, "user": user, "branches": branches, "errors": errors, "form": {
                "from_branch_id": from_branch_id, "to_branch_id": to_branch_id,
                "amount": amount, "clearing_date": clearing_date, "remark": remark
            }},
        )

    today = date.today().isoformat()
    cursor.execute(
        "INSERT INTO clearings (from_branch_id, to_branch_id, bill_id, amount, clearing_date, clearing_type, status, operator_id, remark, created_at) VALUES (?, ?, NULL, ?, ?, '往来登记', '待清算', ?, ?, ?)",
        (from_branch_id, to_branch_id, amount, clearing_date, user["id"], remark, today),
    )
    conn.commit()
    conn.close()
    return RedirectResponse("/clearings", status_code=303)


@app.post("/clearings/{clearing_id}/settle")
def clearing_settle(request: Request, clearing_id: int, user=Depends(auth_dep)):
    if user["role"] not in MANAGER_ROLES:
        raise HTTPException(status_code=403, detail="仅掌柜可操作清算")

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM clearings WHERE id = ?", (clearing_id,))
    clearing = cursor.fetchone()
    if not clearing:
        conn.close()
        raise HTTPException(status_code=404, detail="清算记录不存在")
    if clearing["status"] != "待清算":
        conn.close()
        raise HTTPException(status_code=400, detail="仅待清算记录可操作")

    cursor.execute("UPDATE clearings SET status = '已清算' WHERE id = ?", (clearing_id,))
    if clearing["bill_id"]:
        add_timeline(clearing["bill_id"], "清算", user["real_name"], f"清算完成，金额 {clearing['amount']} 两", conn)
    conn.commit()
    conn.close()
    return RedirectResponse("/clearings", status_code=303)


@app.post("/clearings/{clearing_id}/reconcile")
def clearing_reconcile(request: Request, clearing_id: int, user=Depends(auth_dep)):
    if user["role"] not in ("总号掌柜", "稽核员"):
        raise HTTPException(status_code=403, detail="仅总号掌柜或稽核员可对账")

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM clearings WHERE id = ?", (clearing_id,))
    clearing = cursor.fetchone()
    if not clearing:
        conn.close()
        raise HTTPException(status_code=404, detail="清算记录不存在")
    if clearing["status"] != "已清算":
        conn.close()
        raise HTTPException(status_code=400, detail="仅已清算记录可对账")

    cursor.execute("UPDATE clearings SET status = '已对账' WHERE id = ?", (clearing_id,))
    if clearing["bill_id"]:
        add_timeline(clearing["bill_id"], "对账", user["real_name"], f"对账完成，金额 {clearing['amount']} 两", conn)
    conn.commit()
    conn.close()
    return RedirectResponse("/clearings", status_code=303)


@app.get("/credit-limits", response_class=HTMLResponse)
def credit_limits_list(request: Request, user=Depends(auth_dep)):
    if user["role"] not in ("总号掌柜", "稽核员"):
        raise HTTPException(status_code=403, detail="仅总号掌柜或稽核员可查看额度")
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT cl.*, u.real_name as user_name, b.branch_name FROM credit_limits cl LEFT JOIN users u ON cl.target_type = '经办人' AND cl.target_id = u.id LEFT JOIN branches b ON cl.target_type IN ('分号', '掌柜') AND cl.target_id = CASE WHEN cl.target_type = '分号' THEN b.id WHEN cl.target_type = '掌柜' THEN b.id END ORDER BY cl.id"
    )
    limits_raw = [dict(row) for row in cursor.fetchall()]

    limits = []
    for row in limits_raw:
        if row["target_type"] == "掌柜":
            cursor.execute("SELECT real_name FROM users WHERE id = ?", (row["target_id"],))
            u = cursor.fetchone()
            cursor.execute("SELECT branch_name FROM branches WHERE id = ?", (row["target_id"],))
            b = cursor.fetchone()
            row["display_name"] = f"{u['real_name'] if u else ''}（掌柜）"
        elif row["target_type"] == "经办人":
            cursor.execute("SELECT real_name FROM users WHERE id = ?", (row["target_id"],))
            u = cursor.fetchone()
            row["display_name"] = u["real_name"] if u else ""
        elif row["target_type"] == "分号":
            cursor.execute("SELECT branch_name FROM branches WHERE id = ?", (row["target_id"],))
            b = cursor.fetchone()
            row["display_name"] = b["branch_name"] if b else ""
        limits.append(row)

    conn.close()
    return templates.TemplateResponse(
        "credit_limits.html",
        {"request": request, "user": user, "limits": limits},
    )


@app.get("/credit-limits/{limit_id}/edit", response_class=HTMLResponse)
def credit_limit_edit_page(request: Request, limit_id: int, user=Depends(auth_dep)):
    if user["role"] != "总号掌柜":
        raise HTTPException(status_code=403, detail="仅总号掌柜可修改额度")
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM credit_limits WHERE id = ?", (limit_id,))
    limit = cursor.fetchone()
    conn.close()
    if not limit:
        raise HTTPException(status_code=404, detail="额度记录不存在")
    return templates.TemplateResponse(
        "credit_limit_edit.html",
        {"request": request, "user": user, "limit": dict(limit)},
    )


@app.post("/credit-limits/{limit_id}/edit")
def credit_limit_edit_submit(
    request: Request,
    limit_id: int,
    daily_issue_limit: float = Form(...),
    single_redeem_limit: float = Form(...),
    balance_warning: float = Form(...),
    user=Depends(auth_dep),
):
    if user["role"] != "总号掌柜":
        raise HTTPException(status_code=403, detail="仅总号掌柜可修改额度")

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE credit_limits SET daily_issue_limit = ?, single_redeem_limit = ?, balance_warning = ?, updated_at = ? WHERE id = ?",
        (daily_issue_limit, single_redeem_limit, balance_warning, date.today().isoformat(), limit_id),
    )
    conn.commit()
    conn.close()
    return RedirectResponse("/credit-limits", status_code=303)


@app.get("/search", response_class=HTMLResponse)
def search_page(request: Request, user=Depends(auth_dep)):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM branches WHERE status = '营业中' ORDER BY id")
    branches = [dict(row) for row in cursor.fetchall()]
    cursor.execute("SELECT DISTINCT real_name, id FROM users WHERE status = '在职' ORDER BY id")
    operators = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return templates.TemplateResponse(
        "search.html",
        {"request": request, "user": user, "branches": branches, "operators": operators, "results": None, "form": None},
    )


@app.post("/search", response_class=HTMLResponse)
async def search_submit(request: Request, user=Depends(auth_dep)):
    conn = get_db()
    cursor = conn.cursor()

    body = {}
    form_data = await request.form()
    for key in ["bill_no", "status", "amount_min", "amount_max", "date_from", "date_to", "issuer_id", "issue_branch_id"]:
        body[key] = form_data.get(key, "")

    conditions = []
    params = []

    if body["bill_no"]:
        conditions.append("b.bill_no LIKE ?")
        params.append(f"%{body['bill_no']}%")
    if body["status"]:
        conditions.append("b.status = ?")
        params.append(body["status"])
    if body["amount_min"]:
        conditions.append("b.amount >= ?")
        params.append(float(body["amount_min"]))
    if body["amount_max"]:
        conditions.append("b.amount <= ?")
        params.append(float(body["amount_max"]))
    if body["date_from"]:
        conditions.append("b.issue_date >= ?")
        params.append(body["date_from"])
    if body["date_to"]:
        conditions.append("b.issue_date <= ?")
        params.append(body["date_to"])
    if body["issuer_id"]:
        conditions.append("b.issuer_id = ?")
        params.append(int(body["issuer_id"]))
    if body["issue_branch_id"]:
        conditions.append("b.issue_branch_id = ?")
        params.append(int(body["issue_branch_id"]))

    where_clause = " AND ".join(conditions) if conditions else "1=1"
    sql = f"SELECT b.*, u.real_name as issuer_name, br.branch_name as issue_branch_name FROM bills b LEFT JOIN users u ON b.issuer_id = u.id LEFT JOIN branches br ON b.issue_branch_id = br.id WHERE {where_clause} ORDER BY b.id DESC"
    cursor.execute(sql, params)
    results = [dict(row) for row in cursor.fetchall()]

    cursor.execute("SELECT * FROM branches WHERE status = '营业中' ORDER BY id")
    branches = [dict(row) for row in cursor.fetchall()]
    cursor.execute("SELECT DISTINCT real_name, id FROM users WHERE status = '在职' ORDER BY id")
    operators = [dict(row) for row in cursor.fetchall()]
    conn.close()

    return templates.TemplateResponse(
        "search.html",
        {"request": request, "user": user, "branches": branches, "operators": operators, "results": results, "form": body},
    )


@app.get("/finance/loans", response_class=HTMLResponse)
def finance_loans_list(request: Request, status: Optional[str] = None, user=Depends(auth_dep)):
    conn = get_db()
    cursor = conn.cursor()
    sql = """
        SELECT fl.*, b.bill_no, b.amount as bill_amount,
               u1.real_name as applicant_name, u2.real_name as reviewer_name, u3.real_name as approver_name,
               br.branch_name
        FROM finance_loans fl
        LEFT JOIN bills b ON fl.bill_id = b.id
        LEFT JOIN users u1 ON fl.applicant_id = u1.id
        LEFT JOIN users u2 ON fl.reviewer_id = u2.id
        LEFT JOIN users u3 ON fl.approver_id = u3.id
        LEFT JOIN branches br ON fl.branch_id = br.id
    """
    params = []
    if status and status in ('待审核', '已复核', '已放款', '还款中', '已结清', '已逾期', '已拒绝', '已取消'):
        sql += " WHERE fl.status = ?"
        params.append(status)
    sql += " ORDER BY fl.id DESC"
    cursor.execute(sql, params)
    loans = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return templates.TemplateResponse(
        "finance_loan_list.html",
        {"request": request, "user": user, "loans": loans, "current_status": status},
    )


@app.get("/finance/loans/apply", response_class=HTMLResponse)
def finance_loan_apply_page(request: Request, bill_id: Optional[int] = None, user=Depends(auth_dep)):
    if user["role"] not in OPERATOR_ROLES:
        raise HTTPException(status_code=403, detail="仅经办人或掌柜可申请押汇")
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM bills WHERE status = '有效' ORDER BY id DESC")
    bills = [dict(row) for row in cursor.fetchall()]
    cursor.execute("SELECT * FROM customer_credits WHERE status = '生效' ORDER BY id")
    credits = [dict(row) for row in cursor.fetchall()]
    cursor.execute("SELECT * FROM branches WHERE status = '营业中' ORDER BY id")
    branches = [dict(row) for row in cursor.fetchall()]
    selected_bill = None
    if bill_id:
        cursor.execute("SELECT * FROM bills WHERE id = ?", (bill_id,))
        row = cursor.fetchone()
        if row:
            selected_bill = dict(row)
    conn.close()
    return templates.TemplateResponse(
        "finance_loan_apply.html",
        {"request": request, "user": user, "bills": bills, "credits": credits, "branches": branches, "selected_bill": selected_bill},
    )


@app.post("/finance/loans/apply")
def finance_loan_apply_submit(
    request: Request,
    bill_id: int = Form(...),
    customer_name: str = Form(...),
    loan_amount: float = Form(...),
    rate_annual: float = Form(...),
    due_date: str = Form(...),
    branch_id: str = Form(default=""),
    remark: str = Form(default=""),
    user=Depends(auth_dep),
):
    if user["role"] not in OPERATOR_ROLES:
        raise HTTPException(status_code=403, detail="仅经办人或掌柜可申请押汇")
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM bills WHERE id = ?", (bill_id,))
    bill = cursor.fetchone()
    errors = []
    if not bill:
        errors.append("汇票不存在")
    elif bill["status"] != "有效":
        errors.append("仅有效汇票可申请押汇")
    if loan_amount <= 0:
        errors.append("融资金额必须大于零")
    if bill and loan_amount > bill["amount"]:
        errors.append(f"融资金额不能超过票面金额（{bill['amount']} 两）")
    if rate_annual <= 0:
        errors.append("年利率必须大于零")
    cursor.execute(
        "SELECT id FROM finance_loans WHERE bill_id = ? AND status NOT IN ('已结清', '已拒绝', '已取消')",
        (bill_id,),
    )
    if cursor.fetchone():
        errors.append("该汇票已有进行中的押汇融资申请")
    cursor.execute(
        "SELECT * FROM customer_credits WHERE customer_name = ? AND status = '生效'",
        (customer_name,),
    )
    credit_row = cursor.fetchone()
    if credit_row:
        if credit_row["credit_limit"] > 0 and credit_row["used_limit"] + loan_amount > credit_row["credit_limit"]:
            errors.append(f"客户授信额度不足（总额 {credit_row['credit_limit']} 两，已用 {credit_row['used_limit']} 两，本次 {loan_amount} 两）")
    b_id = int(branch_id) if branch_id else user.get("branch_id")
    if errors:
        cursor.execute("SELECT * FROM bills WHERE status = '有效' ORDER BY id DESC")
        bills_list = [dict(row) for row in cursor.fetchall()]
        cursor.execute("SELECT * FROM customer_credits WHERE status = '生效' ORDER BY id")
        credits_list = [dict(row) for row in cursor.fetchall()]
        cursor.execute("SELECT * FROM branches WHERE status = '营业中' ORDER BY id")
        branches = [dict(row) for row in cursor.fetchall()]
        conn.close()
        return templates.TemplateResponse(
            "finance_loan_apply.html",
            {"request": request, "user": user, "bills": bills_list, "credits": credits_list, "branches": branches, "selected_bill": None, "errors": errors, "form": {
                "bill_id": bill_id, "customer_name": customer_name, "loan_amount": loan_amount, "rate_annual": rate_annual, "due_date": due_date, "branch_id": branch_id, "remark": remark
            }},
        )
    today = date.today().isoformat()
    cursor.execute(
        "INSERT INTO finance_loans (bill_id, customer_name, loan_amount, rate_annual, due_date, status, applicant_id, branch_id, remark, created_at) VALUES (?, ?, ?, ?, ?, '待审核', ?, ?, ?, ?)",
        (bill_id, customer_name, loan_amount, rate_annual, due_date, user["id"], b_id, remark, today),
    )
    loan_id = cursor.lastrowid
    add_timeline(bill_id, "押汇申请", user["real_name"], f"申请押汇融资 {loan_amount} 两，客户 {customer_name}，年利率 {rate_annual}%，到期 {due_date}", conn)
    conn.commit()
    conn.close()
    return RedirectResponse(f"/finance/loans/{loan_id}", status_code=303)


@app.get("/finance/loans/{loan_id}", response_class=HTMLResponse)
def finance_loan_detail(request: Request, loan_id: int, user=Depends(auth_dep)):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        """SELECT fl.*, b.bill_no, b.amount as bill_amount, b.issue_date, b.due_date as bill_due_date,
                  u1.real_name as applicant_name, u2.real_name as reviewer_name, u3.real_name as approver_name,
                  br.branch_name
           FROM finance_loans fl
           LEFT JOIN bills b ON fl.bill_id = b.id
           LEFT JOIN users u1 ON fl.applicant_id = u1.id
           LEFT JOIN users u2 ON fl.reviewer_id = u2.id
           LEFT JOIN users u3 ON fl.approver_id = u3.id
           LEFT JOIN branches br ON fl.branch_id = br.id
           WHERE fl.id = ?""",
        (loan_id,),
    )
    loan = cursor.fetchone()
    if not loan:
        conn.close()
        raise HTTPException(status_code=404, detail="押汇融资记录不存在")
    loan_dict = dict(loan)
    cursor.execute(
        "SELECT ia.*, u.real_name as operator_name FROM interest_accruals ia LEFT JOIN users u ON ia.operator_id = u.id WHERE ia.loan_id = ? ORDER BY ia.id",
        (loan_id,),
    )
    accruals = [dict(row) for row in cursor.fetchall()]
    cursor.execute(
        "SELECT cr.*, u.real_name as operator_name FROM collection_reminders cr LEFT JOIN users u ON cr.operator_id = u.id WHERE cr.loan_id = ? ORDER BY cr.id",
        (loan_id,),
    )
    reminders = [dict(row) for row in cursor.fetchall()]
    cursor.execute(
        "SELECT orv.*, u.real_name as operator_name FROM overdue_recoveries orv LEFT JOIN users u ON orv.operator_id = u.id WHERE orv.loan_id = ? ORDER BY orv.id",
        (loan_id,),
    )
    recoveries = [dict(row) for row in cursor.fetchall()]
    cursor.execute(
        "SELECT bd.*, u.real_name as operator_name FROM bad_debts bd LEFT JOIN users u ON bd.operator_id = u.id WHERE bd.loan_id = ? ORDER BY bd.id",
        (loan_id,),
    )
    bad_debts = [dict(row) for row in cursor.fetchall()]
    cursor.execute(
        "SELECT * FROM timeline WHERE bill_id = ? ORDER BY id", (loan_dict["bill_id"],)
    )
    timeline = [dict(row) for row in cursor.fetchall()]
    can_review = loan_dict["status"] == "待审核" and user["role"] in REVIEWER_ROLES
    can_approve = loan_dict["status"] == "已复核" and user["role"] in MANAGER_ROLES
    can_repay = loan_dict["status"] in ("已放款", "还款中") and user["role"] in OPERATOR_ROLES
    can_mark_overdue = loan_dict["status"] in ("已放款", "还款中") and user["role"] in MANAGER_ROLES
    can_settle = loan_dict["status"] in ("已放款", "还款中", "已逾期") and user["role"] in MANAGER_ROLES
    can_accrue = loan_dict["status"] in ("已放款", "还款中", "已逾期") and user["role"] in OPERATOR_ROLES
    can_remind = loan_dict["status"] in ("已放款", "还款中", "已逾期") and user["role"] in OPERATOR_ROLES
    can_recover = loan_dict["status"] == "已逾期" and user["role"] in MANAGER_ROLES
    can_bad_debt = loan_dict["status"] == "已逾期" and user["role"] in MANAGER_ROLES
    conn.close()
    loan_dict["accruals"] = accruals
    loan_dict["reminders"] = reminders
    loan_dict["recoveries"] = recoveries
    loan_dict["bad_debts"] = bad_debts
    return templates.TemplateResponse(
        "finance_loan_detail.html",
        {
            "request": request, "user": user, "loan": loan_dict, "timeline": timeline,
            "can_review": can_review, "can_approve": can_approve, "can_repay": can_repay,
            "can_mark_overdue": can_mark_overdue, "can_settle": can_settle,
            "can_accrue": can_accrue, "can_remind": can_remind,
            "can_recover": can_recover, "can_bad_debt": can_bad_debt,
        },
    )


@app.get("/finance/loans/{loan_id}/review", response_class=HTMLResponse)
def finance_loan_review_page(request: Request, loan_id: int, user=Depends(auth_dep)):
    if user["role"] not in REVIEWER_ROLES:
        raise HTTPException(status_code=403, detail="仅复核人或掌柜可审核")
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        """SELECT fl.*, b.bill_no, b.amount as bill_amount, b.issue_date, b.due_date as bill_due_date,
                  u1.real_name as applicant_name, br.branch_name
           FROM finance_loans fl
           LEFT JOIN bills b ON fl.bill_id = b.id
           LEFT JOIN users u1 ON fl.applicant_id = u1.id
           LEFT JOIN branches br ON fl.branch_id = br.id
           WHERE fl.id = ?""",
        (loan_id,),
    )
    loan = cursor.fetchone()
    conn.close()
    if not loan:
        raise HTTPException(status_code=404, detail="押汇融资记录不存在")
    if loan["status"] != "待审核":
        raise HTTPException(status_code=400, detail="该申请已处理")
    return templates.TemplateResponse(
        "finance_loan_review.html",
        {"request": request, "user": user, "loan": dict(loan)},
    )


@app.post("/finance/loans/{loan_id}/review")
def finance_loan_review_submit(
    request: Request,
    loan_id: int,
    action: str = Form(...),
    review_comment: str = Form(default=""),
    user=Depends(auth_dep),
):
    if user["role"] not in REVIEWER_ROLES:
        raise HTTPException(status_code=403, detail="仅复核人或掌柜可审核")
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT fl.*, b.bill_no FROM finance_loans fl LEFT JOIN bills b ON fl.bill_id = b.id WHERE fl.id = ?",
        (loan_id,),
    )
    loan = cursor.fetchone()
    if not loan:
        conn.close()
        raise HTTPException(status_code=404, detail="押汇融资记录不存在")
    if loan["status"] != "待审核":
        conn.close()
        raise HTTPException(status_code=400, detail="该申请已处理")
    today = date.today().isoformat()
    if action == "approve":
        cursor.execute(
            "UPDATE finance_loans SET status = '已复核', reviewer_id = ?, review_date = ?, review_comment = ? WHERE id = ?",
            (user["id"], today, review_comment, loan_id),
        )
        add_timeline(loan["bill_id"], "押汇复核通过", user["real_name"], f"押汇融资复核通过，金额 {loan['loan_amount']} 两" + (f"，意见：{review_comment}" if review_comment else ""), conn)
    else:
        cursor.execute(
            "UPDATE finance_loans SET status = '已拒绝', reviewer_id = ?, review_date = ?, review_comment = ? WHERE id = ?",
            (user["id"], today, review_comment, loan_id),
        )
        add_timeline(loan["bill_id"], "押汇复核拒绝", user["real_name"], f"押汇融资复核拒绝，汇票 {loan['bill_no']}" + (f"，原因：{review_comment}" if review_comment else ""), conn)
    conn.commit()
    conn.close()
    return RedirectResponse(f"/finance/loans/{loan_id}", status_code=303)


@app.post("/finance/loans/{loan_id}/approve")
def finance_loan_approve(
    request: Request,
    loan_id: int,
    approve_comment: str = Form(default=""),
    user=Depends(auth_dep),
):
    if user["role"] not in MANAGER_ROLES:
        raise HTTPException(status_code=403, detail="仅掌柜可审批放款")
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT fl.*, b.bill_no FROM finance_loans fl LEFT JOIN bills b ON fl.bill_id = b.id WHERE fl.id = ?",
        (loan_id,),
    )
    loan = cursor.fetchone()
    if not loan:
        conn.close()
        raise HTTPException(status_code=404, detail="押汇融资记录不存在")
    if loan["status"] != "已复核":
        conn.close()
        raise HTTPException(status_code=400, detail="仅已复核的申请可放款")
    today = date.today().isoformat()
    cursor.execute(
        "UPDATE finance_loans SET status = '已放款', approver_id = ?, approve_date = ?, approve_comment = ?, loan_date = ? WHERE id = ?",
        (user["id"], today, approve_comment, today, loan_id),
    )
    add_credit_occupation(loan["customer_name"], loan_id, loan["loan_amount"], "占用", user["id"], "融资放款占用授信", conn)
    add_timeline(loan["bill_id"], "押汇放款", user["real_name"], f"押汇融资放款 {loan['loan_amount']} 两，客户 {loan['customer_name']}" + (f"，审批意见：{approve_comment}" if approve_comment else ""), conn)
    conn.commit()
    conn.close()
    return RedirectResponse(f"/finance/loans/{loan_id}", status_code=303)


@app.post("/finance/loans/{loan_id}/repay")
def finance_loan_repay(
    request: Request,
    loan_id: int,
    repay_principal: float = Form(...),
    repay_interest: float = Form(default=0),
    user=Depends(auth_dep),
):
    if user["role"] not in OPERATOR_ROLES:
        raise HTTPException(status_code=403, detail="无权登记还款")
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM finance_loans WHERE id = ?", (loan_id,))
    loan = cursor.fetchone()
    if not loan:
        conn.close()
        raise HTTPException(status_code=404, detail="押汇融资记录不存在")
    if loan["status"] not in ("已放款", "还款中"):
        conn.close()
        raise HTTPException(status_code=400, detail="当前状态不可还款")
    new_paid = loan["paid_amount"] + repay_principal
    new_interest = loan["interest_paid"] + repay_interest
    new_status = "还款中"
    if new_paid >= loan["loan_amount"]:
        new_status = "已结清"
        new_paid = loan["loan_amount"]
        cursor.execute(
            "UPDATE customer_credits SET used_limit = used_limit - ? WHERE customer_name = ? AND status = '生效'",
            (loan["loan_amount"], loan["customer_name"]),
        )
    cursor.execute(
        "UPDATE finance_loans SET paid_amount = ?, interest_paid = ?, status = ? WHERE id = ?",
        (new_paid, new_interest, new_status, loan_id),
    )
    action_text = "押汇结清" if new_status == "已结清" else "押汇还款"
    add_timeline(loan["bill_id"], action_text, user["real_name"], f"还款本金 {repay_principal} 两，利息 {repay_interest} 两" + ("，已全部结清" if new_status == "已结清" else ""), conn)
    conn.commit()
    conn.close()
    return RedirectResponse(f"/finance/loans/{loan_id}", status_code=303)


@app.post("/finance/loans/{loan_id}/overdue")
def finance_loan_mark_overdue(request: Request, loan_id: int, user=Depends(auth_dep)):
    if user["role"] not in MANAGER_ROLES:
        raise HTTPException(status_code=403, detail="仅掌柜可标记逾期")
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM finance_loans WHERE id = ?", (loan_id,))
    loan = cursor.fetchone()
    if not loan:
        conn.close()
        raise HTTPException(status_code=404, detail="押汇融资记录不存在")
    if loan["status"] not in ("已放款", "还款中"):
        conn.close()
        raise HTTPException(status_code=400, detail="当前状态不可标记逾期")
    cursor.execute("UPDATE finance_loans SET status = '已逾期' WHERE id = ?", (loan_id,))
    add_timeline(loan["bill_id"], "押汇逾期", user["real_name"], f"押汇融资标记逾期，剩余本金 {loan['loan_amount'] - loan['paid_amount']} 两", conn)
    conn.commit()
    conn.close()
    return RedirectResponse(f"/finance/loans/{loan_id}", status_code=303)


@app.post("/finance/loans/{loan_id}/settle")
def finance_loan_settle(request: Request, loan_id: int, user=Depends(auth_dep)):
    if user["role"] not in MANAGER_ROLES:
        raise HTTPException(status_code=403, detail="仅掌柜可结清")
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM finance_loans WHERE id = ?", (loan_id,))
    loan = cursor.fetchone()
    if not loan:
        conn.close()
        raise HTTPException(status_code=404, detail="押汇融资记录不存在")
    if loan["status"] not in ("已放款", "还款中", "已逾期"):
        conn.close()
        raise HTTPException(status_code=400, detail="当前状态不可结清")
    cursor.execute(
        "UPDATE finance_loans SET paid_amount = loan_amount, status = '已结清' WHERE id = ?",
        (loan_id,),
    )
    add_credit_occupation(loan["customer_name"], loan_id, loan["loan_amount"], "释放", user["id"], "强制结清释放授信", conn)
    add_timeline(loan["bill_id"], "押汇结清", user["real_name"], f"押汇融资强制结清，融资金额 {loan['loan_amount']} 两", conn)
    conn.commit()
    conn.close()
    return RedirectResponse(f"/finance/loans/{loan_id}", status_code=303)


@app.get("/customer-credits", response_class=HTMLResponse)
def customer_credits_list(request: Request, user=Depends(auth_dep)):
    if user["role"] not in ("总号掌柜", "分号掌柜", "稽核员"):
        raise HTTPException(status_code=403, detail="仅掌柜或稽核员可查看客户授信")
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT cc.*, br.branch_name FROM customer_credits cc LEFT JOIN branches br ON cc.branch_id = br.id ORDER BY cc.id DESC"
    )
    credits = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return templates.TemplateResponse(
        "customer_credit_list.html",
        {"request": request, "user": user, "credits": credits},
    )


@app.get("/customer-credits/create", response_class=HTMLResponse)
def customer_credit_create_page(request: Request, user=Depends(auth_dep)):
    if user["role"] not in MANAGER_ROLES:
        raise HTTPException(status_code=403, detail="仅掌柜可新增授信")
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM branches WHERE status = '营业中' ORDER BY id")
    branches = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return templates.TemplateResponse(
        "customer_credit_edit.html",
        {"request": request, "user": user, "branches": branches, "credit": None},
    )


@app.post("/customer-credits/create")
def customer_credit_create_submit(
    request: Request,
    customer_name: str = Form(...),
    customer_type: str = Form(...),
    credit_limit: float = Form(...),
    rate_annual: float = Form(default=0),
    branch_id: str = Form(default=""),
    remark: str = Form(default=""),
    user=Depends(auth_dep),
):
    if user["role"] not in MANAGER_ROLES:
        raise HTTPException(status_code=403, detail="仅掌柜可新增授信")
    conn = get_db()
    cursor = conn.cursor()
    today = date.today().isoformat()
    b_id = int(branch_id) if branch_id else None
    cursor.execute(
        "INSERT INTO customer_credits (customer_name, customer_type, credit_limit, rate_annual, branch_id, remark, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (customer_name, customer_type, credit_limit, rate_annual, b_id, remark, today),
    )
    conn.commit()
    conn.close()
    return RedirectResponse("/customer-credits", status_code=303)


@app.get("/customer-credits/{credit_id}/edit", response_class=HTMLResponse)
def customer_credit_edit_page(request: Request, credit_id: int, user=Depends(auth_dep)):
    if user["role"] not in MANAGER_ROLES:
        raise HTTPException(status_code=403, detail="仅掌柜可编辑授信")
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM customer_credits WHERE id = ?", (credit_id,))
    credit = cursor.fetchone()
    cursor.execute("SELECT * FROM branches WHERE status = '营业中' ORDER BY id")
    branches = [dict(row) for row in cursor.fetchall()]
    conn.close()
    if not credit:
        raise HTTPException(status_code=404, detail="授信记录不存在")
    return templates.TemplateResponse(
        "customer_credit_edit.html",
        {"request": request, "user": user, "branches": branches, "credit": dict(credit)},
    )


@app.post("/customer-credits/{credit_id}/edit")
def customer_credit_edit_submit(
    request: Request,
    credit_id: int,
    customer_name: str = Form(...),
    customer_type: str = Form(...),
    credit_limit: float = Form(...),
    rate_annual: float = Form(default=0),
    branch_id: str = Form(default=""),
    status: str = Form(default="生效"),
    remark: str = Form(default=""),
    user=Depends(auth_dep),
):
    if user["role"] not in MANAGER_ROLES:
        raise HTTPException(status_code=403, detail="仅掌柜可编辑授信")
    conn = get_db()
    cursor = conn.cursor()
    today = date.today().isoformat()
    b_id = int(branch_id) if branch_id else None
    cursor.execute(
        "UPDATE customer_credits SET customer_name = ?, customer_type = ?, credit_limit = ?, rate_annual = ?, branch_id = ?, status = ?, remark = ?, updated_at = ? WHERE id = ?",
        (customer_name, customer_type, credit_limit, rate_annual, b_id, status, remark, today, credit_id),
    )
    conn.commit()
    conn.close()
    return RedirectResponse("/customer-credits", status_code=303)


@app.get("/interest-accruals", response_class=HTMLResponse)
def interest_accruals_list(request: Request, loan_id: Optional[int] = None, user=Depends(auth_dep)):
    if user["role"] not in ALL_MANAGE_ROLES:
        raise HTTPException(status_code=403, detail="无权查看利息计提")
    conn = get_db()
    cursor = conn.cursor()
    sql = """
        SELECT ia.*, fl.loan_amount, fl.customer_name, fl.rate_annual, b.bill_no, u.real_name as operator_name
        FROM interest_accruals ia
        LEFT JOIN finance_loans fl ON ia.loan_id = fl.id
        LEFT JOIN bills b ON fl.bill_id = b.id
        LEFT JOIN users u ON ia.operator_id = u.id
    """
    params = []
    if loan_id:
        sql += " WHERE ia.loan_id = ?"
        params.append(loan_id)
    sql += " ORDER BY ia.id DESC"
    cursor.execute(sql, params)
    accruals = [dict(row) for row in cursor.fetchall()]
    cursor.execute(
        "SELECT fl.*, b.bill_no FROM finance_loans fl LEFT JOIN bills b ON fl.bill_id = b.id WHERE fl.status IN ('已放款', '还款中', '已逾期') ORDER BY fl.id DESC"
    )
    active_loans = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return templates.TemplateResponse(
        "interest_accruals.html",
        {"request": request, "user": user, "accruals": accruals, "active_loans": active_loans},
    )


@app.post("/interest-accruals/create")
def interest_accrual_create(
    request: Request,
    loan_id: int = Form(...),
    period_start: str = Form(...),
    period_end: str = Form(...),
    user=Depends(auth_dep),
):
    if user["role"] not in OPERATOR_ROLES:
        raise HTTPException(status_code=403, detail="无权计提利息")
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM finance_loans WHERE id = ?", (loan_id,))
    loan = cursor.fetchone()
    if not loan:
        conn.close()
        raise HTTPException(status_code=404, detail="融资记录不存在")
    if loan["status"] not in ("已放款", "还款中", "已逾期"):
        conn.close()
        raise HTTPException(status_code=400, detail="该融资状态不可计提利息")
    start = date.fromisoformat(period_start)
    end = date.fromisoformat(period_end)
    days = (end - start).days
    if days <= 0:
        conn.close()
        raise HTTPException(status_code=400, detail="计提天数必须大于零")
    principal = loan["loan_amount"] - loan["paid_amount"]
    rate_daily = loan["rate_annual"] / 100 / 360
    interest_amount = round(principal * rate_daily * days, 2)
    today = date.today().isoformat()
    cursor.execute(
        "INSERT INTO interest_accruals (loan_id, period_start, period_end, days, principal, rate_daily, interest_amount, status, operator_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, '待计提', ?, ?)",
        (loan_id, period_start, period_end, days, principal, rate_daily, interest_amount, user["id"], today),
    )
    add_timeline(loan["bill_id"], "利息计提", user["real_name"], f"计提利息 {interest_amount} 两，期间 {period_start} 至 {period_end}，计 {days} 日", conn)
    conn.commit()
    conn.close()
    return RedirectResponse("/interest-accruals", status_code=303)


@app.post("/interest-accruals/{accrual_id}/confirm")
def interest_accrual_confirm(request: Request, accrual_id: int, user=Depends(auth_dep)):
    if user["role"] not in REVIEWER_ROLES:
        raise HTTPException(status_code=403, detail="仅复核人或掌柜可确认计提")
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM interest_accruals WHERE id = ?", (accrual_id,))
    accrual = cursor.fetchone()
    if not accrual:
        conn.close()
        raise HTTPException(status_code=404, detail="计提记录不存在")
    if accrual["status"] != "待计提":
        conn.close()
        raise HTTPException(status_code=400, detail="仅待计提记录可确认")
    cursor.execute("UPDATE interest_accruals SET status = '已计提' WHERE id = ?", (accrual_id,))
    conn.commit()
    conn.close()
    return RedirectResponse("/interest-accruals", status_code=303)


@app.post("/interest-accruals/{accrual_id}/collect")
def interest_accrual_collect(request: Request, accrual_id: int, user=Depends(auth_dep)):
    if user["role"] not in MANAGER_ROLES:
        raise HTTPException(status_code=403, detail="仅掌柜可收取利息")
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT ia.*, fl.bill_id FROM interest_accruals ia LEFT JOIN finance_loans fl ON ia.loan_id = fl.id WHERE ia.id = ?", (accrual_id,))
    accrual = cursor.fetchone()
    if not accrual:
        conn.close()
        raise HTTPException(status_code=404, detail="计提记录不存在")
    if accrual["status"] != "已计提":
        conn.close()
        raise HTTPException(status_code=400, detail="仅已计提记录可收取")
    cursor.execute("UPDATE interest_accruals SET status = '已收取' WHERE id = ?", (accrual_id,))
    cursor.execute(
        "UPDATE finance_loans SET interest_paid = interest_paid + ? WHERE id = ?",
        (accrual["interest_amount"], accrual["loan_id"]),
    )
    add_timeline(accrual["bill_id"], "利息收取", user["real_name"], f"收取利息 {accrual['interest_amount']} 两", conn)
    conn.commit()
    conn.close()
    return RedirectResponse("/interest-accruals", status_code=303)


@app.get("/collections", response_class=HTMLResponse)
def collections_page(request: Request, user=Depends(auth_dep)):
    if user["role"] not in ALL_MANAGE_ROLES:
        raise HTTPException(status_code=403, detail="无权查看催收管理")
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        """SELECT fl.*, b.bill_no, u1.real_name as applicant_name, br.branch_name
           FROM finance_loans fl
           LEFT JOIN bills b ON fl.bill_id = b.id
           LEFT JOIN users u1 ON fl.applicant_id = u1.id
           LEFT JOIN branches br ON fl.branch_id = br.id
           WHERE fl.status IN ('已放款', '还款中', '已逾期')
           ORDER BY CASE WHEN fl.status = '已逾期' THEN 0 ELSE 1 END, fl.due_date ASC"""
    )
    active_loans = [dict(row) for row in cursor.fetchall()]
    cursor.execute(
        """SELECT cr.*, fl.customer_name, b.bill_no, u.real_name as operator_name
           FROM collection_reminders cr
           LEFT JOIN finance_loans fl ON cr.loan_id = fl.id
           LEFT JOIN bills b ON fl.bill_id = b.id
           LEFT JOIN users u ON cr.operator_id = u.id
           ORDER BY cr.id DESC LIMIT 100"""
    )
    reminders = [dict(row) for row in cursor.fetchall()]
    cursor.execute(
        """SELECT orv.*, fl.customer_name, b.bill_no, u.real_name as operator_name
           FROM overdue_recoveries orv
           LEFT JOIN finance_loans fl ON orv.loan_id = fl.id
           LEFT JOIN bills b ON fl.bill_id = b.id
           LEFT JOIN users u ON orv.operator_id = u.id
           ORDER BY orv.id DESC LIMIT 100"""
    )
    recoveries = [dict(row) for row in cursor.fetchall()]
    cursor.execute(
        """SELECT bd.*, fl.customer_name, b.bill_no, u.real_name as operator_name
           FROM bad_debts bd
           LEFT JOIN finance_loans fl ON bd.loan_id = fl.id
           LEFT JOIN bills b ON fl.bill_id = b.id
           LEFT JOIN users u ON bd.operator_id = u.id
           ORDER BY bd.id DESC LIMIT 100"""
    )
    bad_debts = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return templates.TemplateResponse(
        "collections.html",
        {"request": request, "user": user, "active_loans": active_loans, "reminders": reminders, "recoveries": recoveries, "bad_debts": bad_debts},
    )


@app.post("/collections/reminder")
def collection_reminder_create(
    request: Request,
    loan_id: int = Form(...),
    reminder_type: str = Form(...),
    reminder_date: str = Form(...),
    content: str = Form(...),
    user=Depends(auth_dep),
):
    if user["role"] not in OPERATOR_ROLES:
        raise HTTPException(status_code=403, detail="无权登记催收提醒")
    conn = get_db()
    cursor = conn.cursor()
    today = date.today().isoformat()
    cursor.execute(
        "INSERT INTO collection_reminders (loan_id, reminder_type, reminder_date, content, operator_id, status, created_at) VALUES (?, ?, ?, ?, ?, '待处理', ?)",
        (loan_id, reminder_type, reminder_date, content, user["id"], today),
    )
    cursor.execute("SELECT fl.*, b.bill_no FROM finance_loans fl LEFT JOIN bills b ON fl.bill_id = b.id WHERE fl.id = ?", (loan_id,))
    loan = cursor.fetchone()
    if loan:
        add_timeline(loan["bill_id"], f"催收-{reminder_type}", user["real_name"], content, conn)
    conn.commit()
    conn.close()
    return RedirectResponse("/collections", status_code=303)


@app.post("/collections/reminder/{reminder_id}/status")
def collection_reminder_update_status(
    request: Request,
    reminder_id: int,
    status: str = Form(...),
    response: str = Form(default=""),
    user=Depends(auth_dep),
):
    if user["role"] not in OPERATOR_ROLES:
        raise HTTPException(status_code=403, detail="无权更新催收状态")
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("UPDATE collection_reminders SET status = ?, response = ? WHERE id = ?", (status, response, reminder_id))
    conn.commit()
    conn.close()
    return RedirectResponse("/collections", status_code=303)


@app.post("/collections/recovery")
def overdue_recovery_create(
    request: Request,
    loan_id: int = Form(...),
    recovery_type: str = Form(...),
    recovery_amount: float = Form(...),
    recovery_date: str = Form(...),
    remark: str = Form(default=""),
    user=Depends(auth_dep),
):
    if user["role"] not in MANAGER_ROLES:
        raise HTTPException(status_code=403, detail="仅掌柜可登记追偿")
    conn = get_db()
    cursor = conn.cursor()
    today = date.today().isoformat()
    cursor.execute(
        "INSERT INTO overdue_recoveries (loan_id, recovery_type, recovery_amount, recovery_date, operator_id, status, remark, created_at) VALUES (?, ?, ?, ?, ?, '进行中', ?, ?)",
        (loan_id, recovery_type, recovery_amount, recovery_date, user["id"], remark, today),
    )
    cursor.execute("SELECT fl.*, b.bill_no FROM finance_loans fl LEFT JOIN bills b ON fl.bill_id = b.id WHERE fl.id = ?", (loan_id,))
    loan = cursor.fetchone()
    if loan:
        new_paid = loan["paid_amount"] + recovery_amount
        if new_paid >= loan["loan_amount"]:
            cursor.execute("UPDATE finance_loans SET paid_amount = loan_amount, status = '已结清' WHERE id = ?", (loan_id,))
            cursor.execute(
                "UPDATE customer_credits SET used_limit = used_limit - ? WHERE customer_name = ? AND status = '生效'",
                (loan["loan_amount"], loan["customer_name"]),
            )
            add_timeline(loan["bill_id"], "追偿结清", user["real_name"], f"追偿还款 {recovery_amount} 两，融资已结清，方式：{recovery_type}", conn)
        else:
            cursor.execute("UPDATE finance_loans SET paid_amount = ? WHERE id = ?", (new_paid, loan_id))
            add_timeline(loan["bill_id"], "追偿还款", user["real_name"], f"追偿还款 {recovery_amount} 两，方式：{recovery_type}", conn)
    conn.commit()
    conn.close()
    return RedirectResponse("/collections", status_code=303)


@app.post("/collections/recovery/{recovery_id}/status")
def overdue_recovery_update_status(
    request: Request,
    recovery_id: int,
    status: str = Form(...),
    user=Depends(auth_dep),
):
    if user["role"] not in MANAGER_ROLES:
        raise HTTPException(status_code=403, detail="仅掌柜可更新追偿状态")
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("UPDATE overdue_recoveries SET status = ? WHERE id = ?", (status, recovery_id))
    conn.commit()
    conn.close()
    return RedirectResponse("/collections", status_code=303)


@app.post("/collections/bad-debt")
def bad_debt_create(
    request: Request,
    loan_id: int = Form(...),
    principal_remaining: float = Form(...),
    interest_remaining: float = Form(default=0),
    provision_amount: float = Form(default=0),
    provision_ratio: float = Form(default=0),
    bad_debt_date: str = Form(...),
    remark: str = Form(default=""),
    user=Depends(auth_dep),
):
    if user["role"] not in MANAGER_ROLES:
        raise HTTPException(status_code=403, detail="仅掌柜可登记坏账")
    conn = get_db()
    cursor = conn.cursor()
    today = date.today().isoformat()
    cursor.execute(
        "INSERT INTO bad_debts (loan_id, principal_remaining, interest_remaining, provision_amount, provision_ratio, bad_debt_date, operator_id, status, remark, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, '已登记', ?, ?)",
        (loan_id, principal_remaining, interest_remaining, provision_amount, provision_ratio, bad_debt_date, user["id"], remark, today),
    )
    cursor.execute("SELECT fl.*, b.bill_no FROM finance_loans fl LEFT JOIN bills b ON fl.bill_id = b.id WHERE fl.id = ?", (loan_id,))
    loan = cursor.fetchone()
    if loan:
        add_timeline(loan["bill_id"], "坏账登记", user["real_name"], f"坏账登记，剩余本金 {principal_remaining} 两，剩余利息 {interest_remaining} 两", conn)
    conn.commit()
    conn.close()
    return RedirectResponse("/collections", status_code=303)


@app.post("/collections/bad-debt/{bad_debt_id}/status")
def bad_debt_update_status(
    request: Request,
    bad_debt_id: int,
    status: str = Form(...),
    disposal_type: str = Form(default=""),
    disposal_date: str = Form(default=""),
    disposal_amount: float = Form(default=0),
    user=Depends(auth_dep),
):
    if user["role"] != "总号掌柜":
        raise HTTPException(status_code=403, detail="仅总号掌柜可处置坏账")
    conn = get_db()
    cursor = conn.cursor()
    if status in ("已计提准备", "已处置", "已核销"):
        cursor.execute(
            "UPDATE bad_debts SET status = ?, disposal_type = ?, disposal_date = ?, disposal_amount = ? WHERE id = ?",
            (status, disposal_type or None, disposal_date or None, disposal_amount, bad_debt_id),
        )
        if status == "已核销":
            cursor.execute("SELECT bd.*, fl.bill_id, fl.customer_name, fl.loan_amount FROM bad_debts bd LEFT JOIN finance_loans fl ON bd.loan_id = fl.id WHERE bd.id = ?", (bad_debt_id,))
            bd = cursor.fetchone()
            if bd:
                cursor.execute("UPDATE finance_loans SET status = '已结清', paid_amount = loan_amount WHERE id = ?", (bd["loan_id"],))
                add_credit_occupation(bd["customer_name"], bd["loan_id"], bd["loan_amount"], "释放", user["id"], "坏账核销释放授信", conn)
                add_audit_log("核销", bad_debt_id, "坏账核销", user, f"客户 {bd['customer_name']}，金额 {bd['principal_remaining']} 两")
                add_timeline(bd["bill_id"], "坏账核销", user["real_name"], f"坏账核销，核销金额 {bd['principal_remaining']} 两", conn)
    conn.commit()
    conn.close()
    return RedirectResponse("/collections", status_code=303)


@app.get("/reports", response_class=HTMLResponse)
def reports_page(request: Request, user=Depends(auth_dep)):
    if user["role"] not in ALL_MANAGE_ROLES:
        raise HTTPException(status_code=403, detail="无权查看报表")
    return templates.TemplateResponse(
        "reports.html",
        {"request": request, "user": user, "report_data": None},
    )


@app.post("/reports")
def reports_generate(
    request: Request,
    report_type: str = Form(...),
    start_date: str = Form(...),
    end_date: str = Form(...),
    user=Depends(auth_dep),
):
    if user["role"] not in ALL_MANAGE_ROLES:
        raise HTTPException(status_code=403, detail="无权查看报表")

    conn = get_db()
    cursor = conn.cursor()

    report_data = {
        "type": "日" if report_type == "daily" else ("月" if report_type == "monthly" else ("融资" if report_type == "finance" else ("催收" if report_type == "collection" else ("授信" if report_type == "credit" else "风险")))),
        "start_date": start_date,
        "end_date": end_date,
    }

    if report_type in ("daily", "monthly"):
        cursor.execute(
            "SELECT COUNT(*) as cnt, COALESCE(SUM(amount), 0) as total FROM bills WHERE issue_date BETWEEN ? AND ? AND status != '已作废'",
            (start_date, end_date),
        )
        issue_row = cursor.fetchone()
        report_data["issue_count"] = issue_row["cnt"]
        report_data["issue_total"] = issue_row["total"]

        cursor.execute(
            "SELECT COUNT(*) as cnt, COALESCE(SUM(amount), 0) as total FROM redemptions WHERE request_date BETWEEN ? AND ? AND status = '已完成'",
            (start_date, end_date),
        )
        redeem_row = cursor.fetchone()
        report_data["redeem_count"] = redeem_row["cnt"]
        report_data["redeem_total"] = redeem_row["total"]

        cursor.execute(
            "SELECT COUNT(*) as cnt FROM bills WHERE status = '有效'"
        )
        report_data["active_count"] = cursor.fetchone()["cnt"]

        cursor.execute(
            "SELECT COUNT(*) as cnt FROM bills WHERE status IN ('挂失', '冻结')"
        )
        report_data["exception_count"] = cursor.fetchone()["cnt"]

        cursor.execute(
            "SELECT br.branch_name, COUNT(b.id) as cnt, COALESCE(SUM(b.amount), 0) as total FROM bills b LEFT JOIN branches br ON b.issue_branch_id = br.id WHERE b.issue_date BETWEEN ? AND ? AND b.status != '已作废' GROUP BY b.issue_branch_id ORDER BY total DESC",
            (start_date, end_date),
        )
        report_data["branch_stats"] = [dict(row) for row in cursor.fetchall()]

        cursor.execute(
            "SELECT u.real_name, COUNT(b.id) as cnt, COALESCE(SUM(b.amount), 0) as total FROM bills b LEFT JOIN users u ON b.issuer_id = u.id WHERE b.issue_date BETWEEN ? AND ? AND b.status != '已作废' GROUP BY b.issuer_id ORDER BY total DESC",
            (start_date, end_date),
        )
        report_data["operator_stats"] = [dict(row) for row in cursor.fetchall()]

        cursor.execute(
            "SELECT COALESCE(SUM(amount), 0) as total FROM clearings WHERE clearing_date BETWEEN ? AND ? AND status IN ('已清算', '已对账')",
            (start_date, end_date),
        )
        report_data["clearing_total"] = cursor.fetchone()["total"]

    elif report_type == "finance":
        cursor.execute(
            "SELECT COUNT(*) as cnt, COALESCE(SUM(loan_amount), 0) as total, COALESCE(SUM(paid_amount), 0) as paid FROM finance_loans WHERE created_at BETWEEN ? AND ?",
            (start_date, end_date),
        )
        fin_row = cursor.fetchone()
        report_data["total_loans"] = fin_row["cnt"]
        report_data["total_loan_amount"] = fin_row["total"]
        report_data["total_paid"] = fin_row["paid"]
        cursor.execute(
            "SELECT COUNT(*) as cnt FROM finance_loans WHERE status = '已逾期' AND created_at BETWEEN ? AND ?",
            (start_date, end_date),
        )
        report_data["overdue_count"] = cursor.fetchone()["cnt"]
        cursor.execute(
            "SELECT customer_name, COUNT(*) as cnt, COALESCE(SUM(loan_amount), 0) as total_amount, COALESCE(SUM(paid_amount), 0) as total_paid FROM finance_loans WHERE created_at BETWEEN ? AND ? GROUP BY customer_name ORDER BY total_amount DESC",
            (start_date, end_date),
        )
        report_data["customer_stats"] = [dict(row) for row in cursor.fetchall()]
        cursor.execute(
            "SELECT COALESCE(SUM(interest_amount), 0) as total_accrued FROM interest_accruals WHERE created_at BETWEEN ? AND ?",
            (start_date, end_date),
        )
        total_accrued = cursor.fetchone()["total_accrued"]
        cursor.execute(
            "SELECT COALESCE(SUM(interest_amount), 0) as total_collected FROM interest_accruals WHERE status = '已收取' AND created_at BETWEEN ? AND ?",
            (start_date, end_date),
        )
        total_collected = cursor.fetchone()["total_collected"]
        report_data["interest_summary"] = {"total_accrued": total_accrued, "total_collected": total_collected}
        cursor.execute(
            "SELECT br.branch_name, COUNT(fl.id) as cnt, COALESCE(SUM(fl.loan_amount), 0) as total_amount, COALESCE(SUM(fl.loan_amount - fl.paid_amount), 0) as outstanding FROM finance_loans fl LEFT JOIN branches br ON fl.branch_id = br.id WHERE fl.created_at BETWEEN ? AND ? GROUP BY fl.branch_id ORDER BY total_amount DESC",
            (start_date, end_date),
        )
        report_data["branch_finance_stats"] = [dict(row) for row in cursor.fetchall()]
        cursor.execute(
            "SELECT COALESCE(SUM(occupy_amount), 0) as total_occupied FROM credit_occupations WHERE occupy_type = '占用' AND occupy_date BETWEEN ? AND ?",
            (start_date, end_date),
        )
        report_data["credit_occupied"] = cursor.fetchone()["total_occupied"]
        cursor.execute(
            "SELECT COALESCE(SUM(occupy_amount), 0) as total_released FROM credit_occupations WHERE occupy_type = '释放' AND occupy_date BETWEEN ? AND ?",
            (start_date, end_date),
        )
        report_data["credit_released"] = cursor.fetchone()["total_released"]

    elif report_type == "credit":
        cursor.execute(
            "SELECT cc.customer_name, cc.customer_type, cc.credit_limit, cc.used_limit, cc.credit_limit - cc.used_limit as available_limit, cc.rate_annual, cc.status, br.branch_name FROM customer_credits cc LEFT JOIN branches br ON cc.branch_id = br.id WHERE cc.status = '生效' ORDER BY cc.credit_limit DESC"
        )
        report_data["credit_list"] = [dict(row) for row in cursor.fetchall()]
        cursor.execute(
            "SELECT COALESCE(SUM(credit_limit), 0) as total_limit, COALESCE(SUM(used_limit), 0) as total_used FROM customer_credits WHERE status = '生效'"
        )
        cr_row = cursor.fetchone()
        report_data["credit_summary"] = {"total_limit": cr_row["total_limit"], "total_used": cr_row["total_used"], "total_available": cr_row["total_limit"] - cr_row["total_used"]}

    elif report_type == "risk":
        cursor.execute(
            "SELECT crr.customer_name, crr.rating, crr.rating_score, crr.assessment_date, crr.previous_rating, u.real_name as assessor_name FROM customer_risk_ratings crr LEFT JOIN users u ON crr.assessor_id = u.id ORDER BY crr.rating_score DESC"
        )
        report_data["risk_list"] = [dict(row) for row in cursor.fetchall()]
        cursor.execute(
            "SELECT rating, COUNT(*) as cnt, AVG(rating_score) as avg_score FROM customer_risk_ratings GROUP BY rating ORDER BY avg_score DESC"
        )
        report_data["risk_distribution"] = [dict(row) for row in cursor.fetchall()]

    elif report_type == "collection":
        cursor.execute(
            "SELECT COUNT(*) as cnt FROM collection_reminders WHERE reminder_date BETWEEN ? AND ?",
            (start_date, end_date),
        )
        report_data["reminder_count"] = cursor.fetchone()["cnt"]
        cursor.execute(
            "SELECT COUNT(*) as cnt FROM finance_loans WHERE status = '已逾期' AND created_at BETWEEN ? AND ?",
            (start_date, end_date),
        )
        report_data["overdue_loan_count"] = cursor.fetchone()["cnt"]
        cursor.execute(
            "SELECT COUNT(*) as cnt FROM overdue_recoveries WHERE recovery_date BETWEEN ? AND ?",
            (start_date, end_date),
        )
        report_data["recovery_count"] = cursor.fetchone()["cnt"]
        cursor.execute(
            "SELECT COUNT(*) as cnt FROM bad_debts WHERE bad_debt_date BETWEEN ? AND ?",
            (start_date, end_date),
        )
        report_data["bad_debt_count"] = cursor.fetchone()["cnt"]
        cursor.execute(
            "SELECT recovery_type, COUNT(*) as cnt, COALESCE(SUM(recovery_amount), 0) as total_amount FROM overdue_recoveries WHERE recovery_date BETWEEN ? AND ? GROUP BY recovery_type ORDER BY total_amount DESC",
            (start_date, end_date),
        )
        report_data["recovery_stats"] = [dict(row) for row in cursor.fetchall()]
        cursor.execute(
            "SELECT COALESCE(SUM(principal_remaining), 0) as total_principal, COALESCE(SUM(provision_amount), 0) as total_provision, COALESCE(SUM(disposal_amount), 0) as total_disposal FROM bad_debts WHERE bad_debt_date BETWEEN ? AND ?",
            (start_date, end_date),
        )
        bd_row = cursor.fetchone()
        report_data["bad_debt_summary"] = {"total_principal": bd_row["total_principal"], "total_provision": bd_row["total_provision"], "total_disposal": bd_row["total_disposal"]}
        cursor.execute(
            "SELECT COUNT(*) as cnt FROM overdue_warnings WHERE warning_date BETWEEN ? AND ?",
            (start_date, end_date),
        )
        report_data["overdue_warning_count"] = cursor.fetchone()["cnt"]
        cursor.execute(
            "SELECT COUNT(*) as cnt FROM partial_writeoffs WHERE writeoff_date BETWEEN ? AND ?",
            (start_date, end_date),
        )
        report_data["partial_writeoff_count"] = cursor.fetchone()["cnt"]

    conn.close()
    return templates.TemplateResponse(
        "reports.html",
        {"request": request, "user": user, "report_data": report_data},
    )


@app.get("/overdue-warnings", response_class=HTMLResponse)
def overdue_warnings_page(request: Request, status: Optional[str] = None, user=Depends(auth_dep)):
    if user["role"] not in ALL_MANAGE_ROLES:
        raise HTTPException(status_code=403, detail="无权查看逾期预警")
    conn = get_db()
    cursor = conn.cursor()
    check_overdue_loans(conn)
    sql = "SELECT ow.*, fl.customer_name, fl.loan_amount, fl.paid_amount, fl.due_date as loan_due_date, b.bill_no, u.real_name as operator_name FROM overdue_warnings ow LEFT JOIN finance_loans fl ON ow.loan_id = fl.id LEFT JOIN bills b ON fl.bill_id = b.id LEFT JOIN users u ON ow.operator_id = u.id"
    params = []
    if status and status in ("待处理", "已通知", "已处理", "已忽略"):
        sql += " WHERE ow.status = ?"
        params.append(status)
    sql += " ORDER BY ow.id DESC"
    cursor.execute(sql, params)
    warnings_list = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return templates.TemplateResponse(
        "overdue_warnings.html",
        {"request": request, "user": user, "warnings": warnings_list, "current_status": status},
    )


@app.post("/overdue-warnings/check")
def overdue_warnings_check(request: Request, user=Depends(auth_dep)):
    if user["role"] not in ALL_MANAGE_ROLES:
        raise HTTPException(status_code=403, detail="无权执行逾期检查")
    conn = get_db()
    check_overdue_loans(conn)
    conn.commit()
    conn.close()
    return RedirectResponse("/overdue-warnings", status_code=303)


@app.post("/overdue-warnings/{warning_id}/status")
def overdue_warning_update_status(request: Request, warning_id: int, status: str = Form(...), user=Depends(auth_dep)):
    if user["role"] not in OPERATOR_ROLES:
        raise HTTPException(status_code=403, detail="无权更新预警状态")
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("UPDATE overdue_warnings SET status = ?, operator_id = ? WHERE id = ?", (status, user["id"], warning_id))
    add_audit_log("逾期预警", warning_id, f"更新状态为{status}", user, "", conn)
    conn.commit()
    conn.close()
    return RedirectResponse("/overdue-warnings", status_code=303)


@app.get("/batch-collections", response_class=HTMLResponse)
def batch_collections_page(request: Request, user=Depends(auth_dep)):
    if user["role"] not in OPERATOR_ROLES:
        raise HTTPException(status_code=403, detail="无权批量催收")
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT fl.*, b.bill_no, u.real_name as applicant_name, br.branch_name FROM finance_loans fl LEFT JOIN bills b ON fl.bill_id = b.id LEFT JOIN users u ON fl.applicant_id = u.id LEFT JOIN branches br ON fl.branch_id = br.id WHERE fl.status = '已逾期' ORDER BY fl.id DESC"
    )
    overdue_loans = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return templates.TemplateResponse(
        "batch_collections.html",
        {"request": request, "user": user, "overdue_loans": overdue_loans},
    )


@app.post("/batch-collections")
def batch_collections_submit(request: Request, collection_data: str = Form(...), user=Depends(auth_dep)):
    if user["role"] not in OPERATOR_ROLES:
        raise HTTPException(status_code=403, detail="无权批量催收")
    conn = get_db()
    cursor = conn.cursor()
    try:
        items = json.loads(collection_data)
    except json.JSONDecodeError:
        conn.close()
        raise HTTPException(status_code=400, detail="数据格式错误")
    results = []
    errors = []
    for item in items:
        loan_id = int(item.get("loan_id", 0))
        reminder_type = item.get("reminder_type", "逾期催收")
        content_text = item.get("content", "")
        cursor.execute("SELECT * FROM finance_loans WHERE id = ?", (loan_id,))
        loan = cursor.fetchone()
        if not loan:
            errors.append(f"融资记录 {loan_id} 不存在")
            continue
        if loan["status"] != "已逾期":
            errors.append(f"融资记录 {loan_id} 非逾期状态")
            continue
        today = date.today().isoformat()
        cursor.execute(
            "INSERT INTO collection_reminders (loan_id, reminder_type, reminder_date, content, operator_id, status, created_at) VALUES (?, ?, ?, ?, ?, '待处理', ?)",
            (loan_id, reminder_type, today, content_text, user["id"], today),
        )
        add_timeline(loan["bill_id"], f"批量催收-{reminder_type}", user["real_name"], content_text, conn)
        results.append(f"融资 {loan_id}：催收提醒已创建")
    conn.commit()
    conn.close()
    return templates.TemplateResponse(
        "batch_result.html",
        {"request": request, "user": user, "results": results, "errors": errors, "action": "批量催收"},
    )


@app.get("/finance/loans/{loan_id}/extend", response_class=HTMLResponse)
def loan_extend_page(request: Request, loan_id: int, user=Depends(auth_dep)):
    if user["role"] not in OPERATOR_ROLES:
        raise HTTPException(status_code=403, detail="无权申请展期")
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT fl.*, b.bill_no, br.branch_name FROM finance_loans fl LEFT JOIN bills b ON fl.bill_id = b.id LEFT JOIN branches br ON fl.branch_id = br.id WHERE fl.id = ?",
        (loan_id,),
    )
    loan = cursor.fetchone()
    conn.close()
    if not loan:
        raise HTTPException(status_code=404, detail="融资记录不存在")
    if loan["status"] not in ("已放款", "还款中", "已逾期"):
        raise HTTPException(status_code=400, detail="当前状态不可申请展期")
    return templates.TemplateResponse(
        "loan_extend.html",
        {"request": request, "user": user, "loan": dict(loan)},
    )


@app.post("/finance/loans/{loan_id}/extend")
def loan_extend_submit(request: Request, loan_id: int, extension_months: int = Form(...), new_due_date: str = Form(...), extension_reason: str = Form(...), new_rate_annual: str = Form(default=""), remark: str = Form(default=""), user=Depends(auth_dep)):
    if user["role"] not in OPERATOR_ROLES:
        raise HTTPException(status_code=403, detail="无权申请展期")
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM finance_loans WHERE id = ?", (loan_id,))
    loan = cursor.fetchone()
    if not loan:
        conn.close()
        raise HTTPException(status_code=404, detail="融资记录不存在")
    if loan["status"] not in ("已放款", "还款中", "已逾期"):
        conn.close()
        raise HTTPException(status_code=400, detail="当前状态不可申请展期")
    if extension_months <= 0:
        conn.close()
        raise HTTPException(status_code=400, detail="展期月数必须大于零")
    today = date.today().isoformat()
    new_rate = float(new_rate_annual) if new_rate_annual else loan["rate_annual"]
    cursor.execute(
        "INSERT INTO loan_extensions (loan_id, original_due_date, new_due_date, extension_reason, extension_months, new_rate_annual, status, applicant_id, remark, created_at) VALUES (?, ?, ?, ?, ?, ?, '待审核', ?, ?, ?)",
        (loan_id, loan["due_date"], new_due_date, extension_reason, extension_months, new_rate, user["id"], remark, today),
    )
    ext_id = cursor.lastrowid
    add_audit_log("展期", ext_id, "申请展期", user, f"融资 {loan_id}，展期 {extension_months} 月", conn)
    conn.commit()
    conn.close()
    return RedirectResponse("/loan-extensions", status_code=303)


@app.get("/loan-extensions", response_class=HTMLResponse)
def loan_extensions_list(request: Request, user=Depends(auth_dep)):
    if user["role"] not in ALL_MANAGE_ROLES:
        raise HTTPException(status_code=403, detail="无权查看展期记录")
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT le.*, fl.customer_name, fl.loan_amount, fl.due_date as current_due_date, b.bill_no, u1.real_name as applicant_name, u2.real_name as reviewer_name, u3.real_name as approver_name FROM loan_extensions le LEFT JOIN finance_loans fl ON le.loan_id = fl.id LEFT JOIN bills b ON fl.bill_id = b.id LEFT JOIN users u1 ON le.applicant_id = u1.id LEFT JOIN users u2 ON le.reviewer_id = u2.id LEFT JOIN users u3 ON le.approver_id = u3.id ORDER BY le.id DESC"
    )
    extensions = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return templates.TemplateResponse(
        "loan_extensions.html",
        {"request": request, "user": user, "extensions": extensions},
    )


@app.post("/loan-extensions/{ext_id}/review")
def loan_extension_review(request: Request, ext_id: int, action: str = Form(...), review_comment: str = Form(default=""), user=Depends(auth_dep)):
    if user["role"] not in REVIEWER_ROLES:
        raise HTTPException(status_code=403, detail="仅复核人或掌柜可审核展期")
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM loan_extensions WHERE id = ?", (ext_id,))
    ext = cursor.fetchone()
    if not ext:
        conn.close()
        raise HTTPException(status_code=404, detail="展期记录不存在")
    if ext["status"] != "待审核":
        conn.close()
        raise HTTPException(status_code=400, detail="该展期申请已处理")
    today = date.today().isoformat()
    if action == "approve":
        cursor.execute("UPDATE loan_extensions SET status = '已复核', reviewer_id = ?, review_date = ?, review_comment = ? WHERE id = ?", (user["id"], today, review_comment, ext_id))
        add_audit_log("展期", ext_id, "复核通过", user, "", conn)
    else:
        cursor.execute("UPDATE loan_extensions SET status = '已拒绝', reviewer_id = ?, review_date = ?, review_comment = ? WHERE id = ?", (user["id"], today, review_comment, ext_id))
        add_audit_log("展期", ext_id, "复核拒绝", user, review_comment, conn)
    conn.commit()
    conn.close()
    return RedirectResponse("/loan-extensions", status_code=303)


@app.post("/loan-extensions/{ext_id}/approve")
def loan_extension_approve(request: Request, ext_id: int, action: str = Form(...), approve_comment: str = Form(default=""), user=Depends(auth_dep)):
    if user["role"] not in MANAGER_ROLES:
        raise HTTPException(status_code=403, detail="仅掌柜可审批展期")
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT le.*, fl.customer_name FROM loan_extensions le LEFT JOIN finance_loans fl ON le.loan_id = fl.id WHERE le.id = ?", (ext_id,))
    ext = cursor.fetchone()
    if not ext:
        conn.close()
        raise HTTPException(status_code=404, detail="展期记录不存在")
    if ext["status"] != "已复核":
        conn.close()
        raise HTTPException(status_code=400, detail="仅已复核的展期可审批")
    today = date.today().isoformat()
    if action == "approve":
        cursor.execute("UPDATE loan_extensions SET status = '已批准', approver_id = ?, approve_date = ?, approve_comment = ? WHERE id = ?", (user["id"], today, approve_comment, ext_id))
        cursor.execute("UPDATE finance_loans SET due_date = ?, rate_annual = COALESCE(?, rate_annual) WHERE id = ?", (ext["new_due_date"], ext["new_rate_annual"], ext["loan_id"]))
        cursor.execute("UPDATE finance_loans SET status = '还款中' WHERE id = ? AND status = '已逾期'", (ext["loan_id"],))
        add_audit_log("展期", ext_id, "批准展期", user, f"融资 {ext['loan_id']}，新到期日 {ext['new_due_date']}", conn)
    else:
        cursor.execute("UPDATE loan_extensions SET status = '已拒绝', approver_id = ?, approve_date = ?, approve_comment = ? WHERE id = ?", (user["id"], today, approve_comment, ext_id))
        add_audit_log("展期", ext_id, "拒绝展期", user, approve_comment, conn)
    conn.commit()
    conn.close()
    return RedirectResponse("/loan-extensions", status_code=303)


@app.get("/partial-writeoffs", response_class=HTMLResponse)
def partial_writeoffs_list(request: Request, user=Depends(auth_dep)):
    if user["role"] not in ALL_MANAGE_ROLES:
        raise HTTPException(status_code=403, detail="无权查看部分核销")
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT pw.*, fl.customer_name, fl.loan_amount, b.bill_no, u1.real_name as operator_name, u2.real_name as approver_name FROM partial_writeoffs pw LEFT JOIN finance_loans fl ON pw.loan_id = fl.id LEFT JOIN bills b ON fl.bill_id = b.id LEFT JOIN users u1 ON pw.operator_id = u1.id LEFT JOIN users u2 ON pw.approver_id = u2.id ORDER BY pw.id DESC"
    )
    writeoffs = [dict(row) for row in cursor.fetchall()]
    cursor.execute(
        "SELECT bd.*, fl.customer_name, b.bill_no FROM bad_debts bd LEFT JOIN finance_loans fl ON bd.loan_id = fl.id LEFT JOIN bills b ON fl.bill_id = b.id WHERE bd.status IN ('已登记', '已计提准备') ORDER BY bd.id DESC"
    )
    bad_debts_list = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return templates.TemplateResponse(
        "partial_writeoffs.html",
        {"request": request, "user": user, "writeoffs": writeoffs, "bad_debts": bad_debts_list},
    )


@app.post("/partial-writeoffs/create")
def partial_writeoff_create(request: Request, bad_debt_id: int = Form(...), writeoff_principal: float = Form(...), writeoff_interest: float = Form(default=0), writeoff_date: str = Form(...), writeoff_reason: str = Form(...), remark: str = Form(default=""), user=Depends(auth_dep)):
    if user["role"] not in MANAGER_ROLES:
        raise HTTPException(status_code=403, detail="仅掌柜可登记部分核销")
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT bd.*, fl.customer_name, fl.bill_id, fl.loan_amount FROM bad_debts bd LEFT JOIN finance_loans fl ON bd.loan_id = fl.id WHERE bd.id = ?", (bad_debt_id,))
    bd = cursor.fetchone()
    if not bd:
        conn.close()
        raise HTTPException(status_code=404, detail="坏账记录不存在")
    today = date.today().isoformat()
    cursor.execute(
        "INSERT INTO partial_writeoffs (bad_debt_id, loan_id, writeoff_principal, writeoff_interest, writeoff_date, writeoff_reason, operator_id, status, remark, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, '待审批', ?, ?)",
        (bad_debt_id, bd["loan_id"], writeoff_principal, writeoff_interest, writeoff_date, writeoff_reason, user["id"], remark, today),
    )
    wo_id = cursor.lastrowid
    add_audit_log("部分核销", wo_id, "登记部分核销", user, f"坏账 {bad_debt_id}，核销本金 {writeoff_principal} 两", conn)
    conn.commit()
    conn.close()
    return RedirectResponse("/partial-writeoffs", status_code=303)


@app.post("/partial-writeoffs/{wo_id}/approve")
def partial_writeoff_approve(request: Request, wo_id: int, action: str = Form(...), user=Depends(auth_dep)):
    if user["role"] != "总号掌柜":
        raise HTTPException(status_code=403, detail="仅总号掌柜可审批部分核销")
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT pw.*, fl.customer_name, fl.bill_id, fl.loan_amount FROM partial_writeoffs pw LEFT JOIN finance_loans fl ON pw.loan_id = fl.id WHERE pw.id = ?", (wo_id,))
    wo = cursor.fetchone()
    if not wo:
        conn.close()
        raise HTTPException(status_code=404, detail="核销记录不存在")
    if wo["status"] != "待审批":
        conn.close()
        raise HTTPException(status_code=400, detail="该核销申请已处理")
    today = date.today().isoformat()
    if action == "approve":
        cursor.execute("UPDATE partial_writeoffs SET status = '已批准', approver_id = ?, approve_date = ? WHERE id = ?", (user["id"], today, wo_id))
        cursor.execute("UPDATE bad_debts SET principal_remaining = principal_remaining - ?, status = CASE WHEN principal_remaining - ? <= 0 THEN '已核销' ELSE '部分核销' END WHERE id = ?", (wo["writeoff_principal"], wo["writeoff_principal"], wo["bad_debt_id"]))
        add_credit_occupation(wo["customer_name"], wo["loan_id"], wo["writeoff_principal"], "释放", user["id"], "部分核销释放授信", conn)
        add_audit_log("部分核销", wo_id, "批准核销", user, f"核销本金 {wo['writeoff_principal']} 两", conn)
    else:
        cursor.execute("UPDATE partial_writeoffs SET status = '已拒绝', approver_id = ?, approve_date = ? WHERE id = ?", (user["id"], today, wo_id))
        add_audit_log("部分核销", wo_id, "拒绝核销", user, "", conn)
    conn.commit()
    conn.close()
    return RedirectResponse("/partial-writeoffs", status_code=303)


@app.get("/risk-ratings", response_class=HTMLResponse)
def risk_ratings_page(request: Request, user=Depends(auth_dep)):
    if user["role"] not in ALL_MANAGE_ROLES:
        raise HTTPException(status_code=403, detail="无权查看风险评级")
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT crr.*, u.real_name as assessor_name FROM customer_risk_ratings crr LEFT JOIN users u ON crr.assessor_id = u.id ORDER BY crr.id DESC"
    )
    ratings = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return templates.TemplateResponse(
        "risk_ratings.html",
        {"request": request, "user": user, "ratings": ratings},
    )


@app.post("/risk-ratings/create")
def risk_rating_create(request: Request, customer_name: str = Form(...), rating: str = Form(...), rating_score: float = Form(...), assessment_basis: str = Form(default=""), remark: str = Form(default=""), user=Depends(auth_dep)):
    if user["role"] not in MANAGER_ROLES:
        raise HTTPException(status_code=403, detail="仅掌柜可评定风险等级")
    conn = get_db()
    cursor = conn.cursor()
    today = date.today().isoformat()
    cursor.execute("SELECT rating FROM customer_risk_ratings WHERE customer_name = ? ORDER BY id DESC LIMIT 1", (customer_name,))
    prev = cursor.fetchone()
    previous_rating = prev["rating"] if prev else None
    cursor.execute(
        "INSERT INTO customer_risk_ratings (customer_name, rating, rating_score, assessment_basis, assessor_id, assessment_date, previous_rating, remark, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (customer_name, rating, rating_score, assessment_basis, user["id"], today, previous_rating, remark, today),
    )
    add_audit_log("风险评级", cursor.lastrowid, f"评级为{rating}", user, f"客户 {customer_name}，评分 {rating_score}", conn)
    conn.commit()
    conn.close()
    return RedirectResponse("/risk-ratings", status_code=303)


@app.get("/branch-monitor", response_class=HTMLResponse)
def branch_monitor_page(request: Request, user=Depends(auth_dep)):
    if user["role"] not in ALL_MANAGE_ROLES:
        raise HTTPException(status_code=403, detail="无权查看分号监控")
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM branches WHERE status = '营业中' ORDER BY id")
    branches = [dict(row) for row in cursor.fetchall()]
    branch_stats = []
    for branch in branches:
        bid = branch["id"]
        cursor.execute("SELECT COUNT(*) as cnt, COALESCE(SUM(loan_amount), 0) as total_amount FROM finance_loans WHERE branch_id = ? AND status IN ('已放款', '还款中', '已逾期')", (bid,))
        active_row = cursor.fetchone()
        cursor.execute("SELECT COUNT(*) as cnt, COALESCE(SUM(loan_amount - paid_amount), 0) as total_overdue FROM finance_loans WHERE branch_id = ? AND status = '已逾期'", (bid,))
        overdue_row = cursor.fetchone()
        cursor.execute("SELECT COUNT(*) as cnt FROM finance_loans WHERE branch_id = ? AND status = '已结清'", (bid,))
        settled_count = cursor.fetchone()["cnt"]
        cursor.execute("SELECT COALESCE(SUM(paid_amount), 0) as total_paid FROM finance_loans WHERE branch_id = ? AND status IN ('已放款', '还款中', '已逾期')", (bid,))
        total_paid = cursor.fetchone()["total_paid"]
        cursor.execute("SELECT COALESCE(SUM(loan_amount), 0) as total_all FROM finance_loans WHERE branch_id = ?", (bid,))
        total_all = cursor.fetchone()["total_all"]
        branch_stats.append({"id": bid, "branch_name": branch["branch_name"], "branch_code": branch["branch_code"], "active_count": active_row["cnt"], "active_amount": active_row["total_amount"], "overdue_count": overdue_row["cnt"], "overdue_amount": overdue_row["total_overdue"], "settled_count": settled_count, "total_paid": total_paid, "total_all": total_all})
    conn.close()
    return templates.TemplateResponse(
        "branch_monitor.html",
        {"request": request, "user": user, "branch_stats": branch_stats},
    )


@app.get("/audit-logs", response_class=HTMLResponse)
def audit_logs_page(request: Request, business_type: str = "", actor_name: str = "", date_from: str = "", date_to: str = "", user=Depends(auth_dep)):
    if user["role"] not in ALL_MANAGE_ROLES:
        raise HTTPException(status_code=403, detail="无权查看审计日志")
    conn = get_db()
    cursor = conn.cursor()
    conditions = []
    params = []
    if business_type:
        conditions.append("al.business_type = ?")
        params.append(business_type)
    if actor_name:
        conditions.append("al.actor_name LIKE ?")
        params.append(f"%{actor_name}%")
    if date_from:
        conditions.append("al.created_at >= ?")
        params.append(date_from)
    if date_to:
        conditions.append("al.created_at <= ?")
        params.append(date_to)
    where_clause = " AND ".join(conditions) if conditions else "1=1"
    sql = f"SELECT al.* FROM audit_logs al WHERE {where_clause} ORDER BY al.id DESC LIMIT 500"
    cursor.execute(sql, params)
    logs = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return templates.TemplateResponse(
        "audit_logs.html",
        {"request": request, "user": user, "logs": logs, "business_type": business_type, "actor_name": actor_name, "date_from": date_from, "date_to": date_to},
    )


@app.get("/approval-chains", response_class=HTMLResponse)
def approval_chains_page(request: Request, user=Depends(auth_dep)):
    if user["role"] != "总号掌柜":
        raise HTTPException(status_code=403, detail="仅总号掌柜可配置审批链")
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM approval_chains ORDER BY business_type, step_order")
    chains = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return templates.TemplateResponse(
        "approval_chains.html",
        {"request": request, "user": user, "chains": chains},
    )


@app.post("/approval-chains/create")
def approval_chain_create(request: Request, business_type: str = Form(...), amount_threshold: float = Form(default=0), step_order: int = Form(...), role_required: str = Form(...), description: str = Form(default=""), user=Depends(auth_dep)):
    if user["role"] != "总号掌柜":
        raise HTTPException(status_code=403, detail="仅总号掌柜可配置审批链")
    conn = get_db()
    cursor = conn.cursor()
    today = date.today().isoformat()
    cursor.execute(
        "INSERT INTO approval_chains (business_type, amount_threshold, step_order, role_required, description, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (business_type, amount_threshold, step_order, role_required, description, today),
    )
    add_audit_log("审批链", cursor.lastrowid, "新增审批规则", user, f"{business_type} 金额阈值 {amount_threshold} 步骤 {step_order} 角色 {role_required}", conn)
    conn.commit()
    conn.close()
    return RedirectResponse("/approval-chains", status_code=303)



@app.post("/bills/{bill_id}/exception")
def bill_exception(
    request: Request,
    bill_id: int,
    exception_type: str = Form(...),
    reason: str = Form(...),
    user=Depends(auth_dep),
):
    if exception_type == "挂失" and user["role"] not in MANAGER_ROLES:
        raise HTTPException(status_code=403, detail="仅掌柜可挂失")
    if exception_type == "冻结" and user["role"] not in MANAGER_ROLES:
        raise HTTPException(status_code=403, detail="仅掌柜可冻结")
    if exception_type == "解冻" and user["role"] not in MANAGER_ROLES:
        raise HTTPException(status_code=403, detail="仅掌柜可解冻")
    if exception_type == "追回" and user["role"] not in MANAGER_ROLES:
        raise HTTPException(status_code=403, detail="仅掌柜可追回")
    if exception_type == "冲正" and user["role"] != "总号掌柜":
        raise HTTPException(status_code=403, detail="仅总号掌柜可冲正")

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM bills WHERE id = ?", (bill_id,))
    bill = cursor.fetchone()
    if not bill:
        conn.close()
        raise HTTPException(status_code=404, detail="汇票不存在")

    if exception_type == "挂失" and bill["status"] != "有效":
        conn.close()
        raise HTTPException(status_code=400, detail="仅有效汇票可挂失")
    if exception_type == "冻结" and bill["status"] != "有效":
        conn.close()
        raise HTTPException(status_code=400, detail="仅有效汇票可冻结")
    if exception_type == "解冻" and bill["status"] != "冻结":
        conn.close()
        raise HTTPException(status_code=400, detail="仅冻结汇票可解冻")
    if exception_type == "追回" and bill["status"] not in ("挂失", "冻结"):
        conn.close()
        raise HTTPException(status_code=400, detail="仅挂失或冻结汇票可追回")
    if exception_type == "冲正" and bill["status"] != "已兑付":
        conn.close()
        raise HTTPException(status_code=400, detail="仅已兑付汇票可冲正")

    today = date.today().isoformat()
    cursor.execute(
        "INSERT INTO exception_records (bill_id, exception_type, reason, operator_id, operator_name, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (bill_id, exception_type, reason, user["id"], user["real_name"], today),
    )

    if exception_type == "挂失":
        cursor.execute("UPDATE bills SET status = '挂失' WHERE id = ?", (bill_id,))
        add_timeline(bill_id, "挂失", user["real_name"], f"汇票挂失，原因：{reason}", conn)
    elif exception_type == "冻结":
        cursor.execute("UPDATE bills SET status = '冻结' WHERE id = ?", (bill_id,))
        add_timeline(bill_id, "冻结", user["real_name"], f"汇票冻结，原因：{reason}", conn)
    elif exception_type == "解冻":
        cursor.execute("UPDATE bills SET status = '有效' WHERE id = ?", (bill_id,))
        add_timeline(bill_id, "解冻", user["real_name"], f"汇票解冻，原因：{reason}", conn)
    elif exception_type == "追回":
        cursor.execute("UPDATE bills SET status = '有效' WHERE id = ?", (bill_id,))
        add_timeline(bill_id, "追回", user["real_name"], f"汇票追回恢复有效，原因：{reason}", conn)
    elif exception_type == "冲正":
        cursor.execute("UPDATE bills SET status = '有效' WHERE id = ?", (bill_id,))
        cursor.execute(
            "UPDATE redemptions SET status = '已拒绝', review_comment = '冲正撤销' WHERE bill_id = ? AND status = '已完成'",
            (bill_id,),
        )
        add_timeline(bill_id, "冲正", user["real_name"], f"兑付冲正，汇票恢复有效，原因：{reason}", conn)

    conn.commit()
    conn.close()
    return RedirectResponse(f"/bills/{bill_id}", status_code=303)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
