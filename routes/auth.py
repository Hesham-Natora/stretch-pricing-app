# routes/auth.py
from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import (
    LoginManager, UserMixin,
    login_user, logout_user,
    login_required, current_user
)
from werkzeug.security import check_password_hash, generate_password_hash
from functools import wraps

from db import get_db


auth_bp = Blueprint("auth", __name__)

# سنهيّئ LoginManager داخل create_app في pricing_app.py
login_manager = LoginManager()
login_manager.login_view = "auth.login"


class User(UserMixin):
    def __init__(self, id, username, role, sales_type, is_active):
        self.id = id
        self.username = username
        self.role = role
        self.sales_type = sales_type
        self._is_active = is_active

    @property
    def is_active(self):
        return self._is_active


@login_manager.user_loader
def load_user(user_id):
    with get_db() as cur:
        cur.execute(
            """
            SELECT id, username, role, sales_type, is_active
            FROM users
            WHERE id = %s
            """,
            (int(user_id),)
        )
        row = cur.fetchone()

    if not row:
        return None

    return User(
        id=row[0],
        username=row[1],
        role=row[2],
        sales_type=row[3],
        is_active=row[4],
    )


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("pricing.pricing_screen"))

    # نجيب كل اليوزرز الـ active للـ dropdown
    with get_db() as cur:
        cur.execute(
            """
            SELECT username
            FROM users
            WHERE is_active = TRUE
            ORDER BY 
                CASE username
                    WHEN 'admin'         THEN 1
                    WHEN 'owner'         THEN 2
                    WHEN 'sales_manager' THEN 3
                    WHEN 'sales_egypt'   THEN 4
                    WHEN 'sales_foreign' THEN 5
                    ELSE 6
                END,
                username
            """
        )
        rows = cur.fetchall()
    usernames = [r[0] for r in rows]

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        if not username or not password:
            flash("Please select username and enter password", "danger")
            return render_template("login.html", usernames=usernames)

        with get_db() as cur:
            cur.execute(
                """
                SELECT id, username, password_hash, role, sales_type, is_active
                FROM users
                WHERE username = %s
                """,
                (username,)
            )
            row = cur.fetchone()

        if not row:
            flash("Invalid username or password", "danger")
            return render_template("login.html", usernames=usernames)

        user_id, username_db, pwd_hash, role, sales_type, is_active = row

        if not is_active:
            flash("Account is deactivated", "danger")
            return render_template("login.html", usernames=usernames)

        if not check_password_hash(pwd_hash, password):
            flash("Invalid username or password", "danger")
            return render_template("login.html", usernames=usernames)

        user = User(user_id, username_db, role, sales_type, is_active)
        login_user(user)

        next_url = request.args.get("next")
        return redirect(next_url or url_for("pricing.pricing_screen"))

    return render_template("login.html", usernames=usernames)


@auth_bp.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("auth.login"))


def roles_required(*roles):
    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            if not current_user.is_authenticated:
                # خليه يمشي في طريق Flask-Login الطبيعي (redirect للـ login)
                return login_manager.unauthorized()
            if current_user.role not in roles:
                # بدل 403 نودّيه لصفحة التسعير الرئيسية برسالة
                flash("You are not authorized to access this page.", "warning")
                return redirect(url_for("pricing.pricing_screen"))
            return f(*args, **kwargs)
        return wrapper
    return decorator


@auth_bp.route("/change-password-public", methods=["GET", "POST"])
def change_password_public():
    # نجيب كل اليوزرز الـ active للـ dropdown (requester + target)
    with get_db() as cur:
        cur.execute(
            """
            SELECT username, role, sales_type
            FROM users
            WHERE is_active = TRUE
            ORDER BY 
                CASE username
                    WHEN 'admin'         THEN 1
                    WHEN 'owner'         THEN 2
                    WHEN 'sales_manager' THEN 3
                    WHEN 'sales_egypt'   THEN 4
                    WHEN 'sales_foreign' THEN 5
                    ELSE 6
                END,
                username
            """
        )
        rows = cur.fetchall()

    users = [
        {
            "username": r[0],
            "role": r[1],
            "sales_type": r[2],
        }
        for r in rows
    ]
    usernames = [u["username"] for u in users]

    if request.method == "POST":
        requester_username = request.form.get("requester_username", "").strip()
        requester_password = request.form.get("requester_password", "")
        target_username = request.form.get("target_username", "").strip()
        new_password = request.form.get("new_password", "")
        confirm_password = request.form.get("confirm_password", "")

        # تحقق من المدخلات
        if (not requester_username or not requester_password or
                not target_username or not new_password or not confirm_password):
            flash("All fields are required.", "danger")
            return render_template("change_password_public.html",
                                   usernames=usernames)

        if new_password != confirm_password:
            flash("New passwords do not match.", "danger")
            return render_template("change_password_public.html",
                                   usernames=usernames)

        with get_db() as cur:
            # requester
            cur.execute(
                """
                SELECT id, username, password_hash, role, sales_type, is_active
                FROM users
                WHERE username = %s
                """,
                (requester_username,)
            )
            requester_row = cur.fetchone()

            # target
            cur.execute(
                """
                SELECT id, username, password_hash, role, sales_type, is_active
                FROM users
                WHERE username = %s
                """,
                (target_username,)
            )
            target_row = cur.fetchone()

        if not requester_row or not target_row:
            flash("Invalid user(s).", "danger")
            return render_template("change_password_public.html",
                                   usernames=usernames)

        (requester_id,
         requester_username_db,
         requester_pwd_hash,
         requester_role,
         requester_sales_type,
         requester_is_active) = requester_row

        (target_id,
         target_username_db,
         target_pwd_hash,
         target_role,
         target_sales_type,
         target_is_active) = target_row

        # requester لازم يكون active
        if not requester_is_active:
            flash("Requester account is deactivated.", "danger")
            return render_template("change_password_public.html",
                                   usernames=usernames)

        # تحقق من باسورد requester
        if not check_password_hash(requester_pwd_hash, requester_password):
            flash("Requester username or password is incorrect.", "danger")
            return render_template("change_password_public.html",
                                   usernames=usernames)

        # منطق الصلاحيات
        allowed = False

        if requester_role == "admin":
            # admin: يقدر يغيّر أي باسورد لأي user (بما فيهم نفسه)
            allowed = True

        elif requester_role == "owner":
            # owner: يقدر يغيّر باسورد نفسه فقط
            if requester_username_db == target_username_db:
                allowed = True

        elif requester_role == "sales_manager":
            # sales_manager:
            # - يغيّر باسورد نفسه
            # - يغيّر باسورد users اللي دورهم sales فقط
            if requester_username_db == target_username_db:
                allowed = True
            elif target_role == "sales":
                allowed = True

        elif requester_role == "sales":
            # sales: لا يقدر يغيّر باسورد أي حد، ولا حتى نفسه
            allowed = False

        if not allowed:
            flash("You are not allowed to change this password.", "danger")
            return render_template("change_password_public.html",
                                   usernames=usernames)

        # تنفيذ التحديث
        new_hash = generate_password_hash(new_password)

        with get_db() as cur:
            cur.execute(
                """
                UPDATE users
                SET password_hash = %s
                WHERE id = %s
                """,
                (new_hash, target_id)
            )

        flash("Password changed successfully.", "success")
        return redirect(url_for("auth.login"))

    return render_template("change_password_public.html",
                           usernames=usernames)


@auth_bp.route("/change-password", methods=["GET", "POST"])
@login_required
def change_password():
    # تقدر تحتفظ بالراوت الداخلي أو تحذفه لو مش محتاجه،
    # هنا هخلّيه يرجّع 404 بسيط عشان ما يحصلش لَبْس.
    return redirect(url_for("auth.change_password_public"))