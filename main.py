from fastapi import FastAPI, Request, Form, Depends, HTTPException, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from database import get_db, init_db
from datetime import date
from typing import Optional
import uuid

app = FastAPI(title="传统钱庄汇票兑付协作系统")
app.add_middleware(SessionMiddleware, secret_key="qianzhuang-huipiao-secret-2024")

templates = Jinja2Templates(directory="templates")

HIGH_VALUE_THRESHOLD = 10000.0


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


def get_current_payee(bill_id: int) -> str:
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT endorsee FROM endorsements WHERE bill_id = ? ORDER BY id DESC LIMIT 1",
        (bill_id,),
    )
    row = cursor.fetchone()
    conn.close()
    if row:
        return row["endorsee"]
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT payee FROM bills WHERE id = ?", (bill_id,))
    row = cursor.fetchone()
    conn.close()
    return row["payee"] if row else ""


def add_timeline(bill_id: int, action: str, actor: str, detail: str = ""):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO timeline (bill_id, action, actor, action_date, detail) VALUES (?, ?, ?, ?, ?)",
        (bill_id, action, actor, date.today().isoformat(), detail),
    )
    conn.commit()
    conn.close()


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
    cursor.execute("SELECT COUNT(*) as cnt FROM redemptions WHERE status = '待复核'")
    pending_review = cursor.fetchone()["cnt"]
    cursor.execute(
        "SELECT b.*, u.real_name as issuer_name FROM bills b LEFT JOIN users u ON b.issuer_id = u.id ORDER BY b.id DESC LIMIT 10"
    )
    recent_bills = [dict(row) for row in cursor.fetchall()]
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
            "pending_review": pending_review,
            "recent_bills": recent_bills,
        },
    )


@app.get("/bills", response_class=HTMLResponse)
def bills_list(request: Request, user=Depends(auth_dep), status: Optional[str] = None):
    conn = get_db()
    cursor = conn.cursor()
    if status and status in ("有效", "已兑付", "已作废"):
        cursor.execute(
            "SELECT b.*, u.real_name as issuer_name FROM bills b LEFT JOIN users u ON b.issuer_id = u.id WHERE b.status = ? ORDER BY b.id DESC",
            (status,),
        )
    else:
        cursor.execute(
            "SELECT b.*, u.real_name as issuer_name FROM bills b LEFT JOIN users u ON b.issuer_id = u.id ORDER BY b.id DESC"
        )
    bills = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return templates.TemplateResponse(
        "bills.html",
        {"request": request, "user": user, "bills": bills, "current_status": status},
    )


@app.get("/bills/create", response_class=HTMLResponse)
def bill_create_page(request: Request, user=Depends(auth_dep)):
    if user["role"] not in ("掌柜", "票号经办人"):
        raise HTTPException(status_code=403, detail="无权签发汇票")
    return templates.TemplateResponse(
        "bill_create.html", {"request": request, "user": user}
    )


@app.post("/bills/create")
def bill_create(
    request: Request,
    bill_no: str = Form(...),
    amount: float = Form(...),
    payee: str = Form(...),
    issue_date: str = Form(...),
    due_date: str = Form(default=""),
    remark: str = Form(default=""),
    user=Depends(auth_dep),
):
    if user["role"] not in ("掌柜", "票号经办人"):
        raise HTTPException(status_code=403, detail="无权签发汇票")

    errors = []
    if amount <= 0:
        errors.append("票面金额必须大于零")

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM bills WHERE bill_no = ?", (bill_no,))
    if cursor.fetchone():
        errors.append("票号已存在，必须唯一")

    if errors:
        conn.close()
        return templates.TemplateResponse(
            "bill_create.html",
            {"request": request, "user": user, "errors": errors, "form": {
                "bill_no": bill_no, "amount": amount, "payee": payee,
                "issue_date": issue_date, "due_date": due_date, "remark": remark
            }},
        )

    try:
        cursor.execute(
            "INSERT INTO bills (bill_no, amount, issuer_id, payee, issue_date, due_date, status, remark) VALUES (?, ?, ?, ?, ?, ?, '有效', ?)",
            (bill_no, amount, user["id"], payee, issue_date, due_date or None, remark),
        )
        bill_id = cursor.lastrowid
        cursor.execute(
            "INSERT INTO timeline (bill_id, action, actor, action_date, detail) VALUES (?, ?, ?, ?, ?)",
            (bill_id, "签发", user["real_name"], date.today().isoformat(), f"签发汇票 {bill_no}，票面金额 {amount} 两，收款人 {payee}"),
        )
        conn.commit()
        conn.close()
        return RedirectResponse(f"/bills/{bill_id}", status_code=303)
    except Exception as e:
        conn.close()
        return templates.TemplateResponse(
            "bill_create.html",
            {"request": request, "user": user, "errors": [str(e)]},
        )


@app.get("/bills/{bill_id}", response_class=HTMLResponse)
def bill_detail(request: Request, bill_id: int, user=Depends(auth_dep)):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT b.*, u.real_name as issuer_name FROM bills b LEFT JOIN users u ON b.issuer_id = u.id WHERE b.id = ?",
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
        "SELECT r.*, u.real_name as operator_name, rv.real_name as reviewer_name FROM redemptions r LEFT JOIN users u ON r.operator_id = u.id LEFT JOIN users rv ON r.reviewer_id = rv.id WHERE r.bill_id = ? ORDER BY r.id",
        (bill_id,),
    )
    redemptions = [dict(row) for row in cursor.fetchall()]

    cursor.execute(
        "SELECT * FROM timeline WHERE bill_id = ? ORDER BY id", (bill_id,)
    )
    timeline = [dict(row) for row in cursor.fetchall()]

    current_payee = get_current_payee(bill_id)
    conn.close()

    bill_dict["endorsements"] = endorsements
    bill_dict["redemptions"] = redemptions

    can_endorse = bill_dict["status"] == "有效" and user["role"] in ("掌柜", "票号经办人")
    can_redeem = bill_dict["status"] == "有效" and user["role"] in ("掌柜", "票号经办人")
    can_void = bill_dict["status"] == "有效" and user["role"] == "掌柜"

    return templates.TemplateResponse(
        "bill_detail.html",
        {
            "request": request,
            "user": user,
            "bill": bill_dict,
            "endorsements": endorsements,
            "redemptions": redemptions,
            "timeline": timeline,
            "current_payee": current_payee,
            "can_endorse": can_endorse,
            "can_redeem": can_redeem,
            "can_void": can_void,
            "high_value_threshold": HIGH_VALUE_THRESHOLD,
        },
    )


@app.post("/bills/{bill_id}/void")
def bill_void(request: Request, bill_id: int, user=Depends(auth_dep)):
    if user["role"] != "掌柜":
        raise HTTPException(status_code=403, detail="仅掌柜可作废汇票")

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM bills WHERE id = ?", (bill_id,))
    bill = cursor.fetchone()
    if not bill:
        conn.close()
        raise HTTPException(status_code=404, detail="汇票不存在")
    if bill["status"] != "有效":
        conn.close()
        raise HTTPException(status_code=400, detail="仅有效汇票可作废")

    cursor.execute("UPDATE bills SET status = '已作废' WHERE id = ?", (bill_id,))
    add_timeline(bill_id, "作废", user["real_name"], f"汇票 {bill['bill_no']} 已作废")
    conn.commit()
    conn.close()
    return RedirectResponse(f"/bills/{bill_id}", status_code=303)


@app.get("/bills/{bill_id}/endorse", response_class=HTMLResponse)
def endorse_page(request: Request, bill_id: int, user=Depends(auth_dep)):
    if user["role"] not in ("掌柜", "票号经办人"):
        raise HTTPException(status_code=403, detail="无权登记背书")

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM bills WHERE id = ?", (bill_id,))
    bill = cursor.fetchone()
    conn.close()

    if not bill:
        raise HTTPException(status_code=404, detail="汇票不存在")
    if bill["status"] != "有效":
        raise HTTPException(status_code=400, detail="该汇票不可背书")

    current_payee = get_current_payee(bill_id)
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
    if user["role"] not in ("掌柜", "票号经办人"):
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
        errors.append("该汇票不可背书（已作废或已兑付）")

    if endorse_date <= bill["issue_date"]:
        errors.append(f"背书日期必须晚于签发日期（{bill['issue_date']}）")

    current_payee = get_current_payee(bill_id)
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
    )
    conn.commit()
    conn.close()
    return RedirectResponse(f"/bills/{bill_id}", status_code=303)


@app.get("/bills/{bill_id}/redeem", response_class=HTMLResponse)
def redeem_page(request: Request, bill_id: int, user=Depends(auth_dep)):
    if user["role"] not in ("掌柜", "票号经办人"):
        raise HTTPException(status_code=403, detail="无权提交兑付")

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM bills WHERE id = ?", (bill_id,))
    bill = cursor.fetchone()
    conn.close()

    if not bill:
        raise HTTPException(status_code=404, detail="汇票不存在")
    if bill["status"] != "有效":
        raise HTTPException(status_code=400, detail="该汇票不可兑付")

    current_payee = get_current_payee(bill_id)
    return templates.TemplateResponse(
        "redeem.html",
        {
            "request": request,
            "user": user,
            "bill": dict(bill),
            "current_payee": current_payee,
        },
    )


@app.post("/bills/{bill_id}/redeem")
def redeem_submit(
    request: Request,
    bill_id: int,
    amount: float = Form(...),
    request_date: str = Form(...),
    user=Depends(auth_dep),
):
    if user["role"] not in ("掌柜", "票号经办人"):
        raise HTTPException(status_code=403, detail="无权提交兑付")

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM bills WHERE id = ?", (bill_id,))
    bill = cursor.fetchone()

    if not bill:
        conn.close()
        raise HTTPException(status_code=404, detail="汇票不存在")

    current_payee = get_current_payee(bill_id)

    errors = []
    if bill["status"] != "有效":
        errors.append("该汇票不可兑付")

    if amount <= 0:
        errors.append("兑付金额必须大于零")

    if amount > bill["amount"]:
        errors.append(f"兑付金额不能超过票面金额（{bill['amount']} 两）")

    if errors:
        conn.close()
        return templates.TemplateResponse(
            "redeem.html",
            {
                "request": request,
                "user": user,
                "bill": dict(bill),
                "current_payee": current_payee,
                "errors": errors,
                "form": {"amount": amount, "request_date": request_date},
            },
        )

    is_high_value = bill["amount"] >= HIGH_VALUE_THRESHOLD

    if not is_high_value:
        cursor.execute(
            "INSERT INTO redemptions (bill_id, payee, amount, request_date, operator_id, status, reviewer_id, review_date) VALUES (?, ?, ?, ?, ?, '已完成', NULL, ?)",
            (bill_id, current_payee, amount, request_date, user["id"], date.today().isoformat()),
        )
        cursor.execute("UPDATE bills SET status = '已兑付' WHERE id = ?", (bill_id,))
        add_timeline(
            bill_id,
            "兑付",
            user["real_name"],
            f"兑付完成，金额 {amount} 两，收款人 {current_payee}",
        )
    else:
        cursor.execute(
            "INSERT INTO redemptions (bill_id, payee, amount, request_date, operator_id, status) VALUES (?, ?, ?, ?, ?, '待复核')",
            (bill_id, current_payee, amount, request_date, user["id"]),
        )
        add_timeline(
            bill_id,
            "兑付申请",
            user["real_name"],
            f"提交兑付申请，金额 {amount} 两，收款人 {current_payee}（高额汇票，待复核）",
        )

    conn.commit()
    conn.close()
    return RedirectResponse(f"/bills/{bill_id}", status_code=303)


@app.get("/redemptions", response_class=HTMLResponse)
def redemptions_list(request: Request, user=Depends(auth_dep)):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT r.*, b.bill_no, b.amount as bill_amount, u.real_name as operator_name FROM redemptions r LEFT JOIN bills b ON r.bill_id = b.id LEFT JOIN users u ON r.operator_id = u.id ORDER BY r.id DESC"
    )
    redemptions = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return templates.TemplateResponse(
        "redemptions.html",
        {"request": request, "user": user, "redemptions": redemptions},
    )


@app.get("/redemptions/{redemption_id}/review", response_class=HTMLResponse)
def review_page(request: Request, redemption_id: int, user=Depends(auth_dep)):
    if user["role"] != "复核人":
        raise HTTPException(status_code=403, detail="仅复核人可审核")

    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT r.*, b.bill_no, b.amount as bill_amount, b.issue_date, u.real_name as operator_name FROM redemptions r LEFT JOIN bills b ON r.bill_id = b.id LEFT JOIN users u ON r.operator_id = u.id WHERE r.id = ?",
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
    if user["role"] != "复核人":
        raise HTTPException(status_code=403, detail="仅复核人可审核")

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
        )
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
        )

    conn.commit()
    conn.close()
    return RedirectResponse("/redemptions", status_code=303)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
