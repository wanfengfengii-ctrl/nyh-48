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
        "type": "日" if report_type == "daily" else "月",
        "start_date": start_date,
        "end_date": end_date,
    }

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

    conn.close()
    return templates.TemplateResponse(
        "reports.html",
        {"request": request, "user": user, "report_data": report_data},
    )


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
