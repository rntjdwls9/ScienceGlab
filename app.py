import json
import os
import sqlite3
import uuid
from datetime import datetime
from functools import wraps
from pathlib import Path

from flask import (
    Flask, g, render_template, request, redirect, url_for, flash, abort,
    jsonify, session, send_from_directory
)
from werkzeug.security import generate_password_hash, check_password_hash

BASE_DIR = Path(__file__).resolve().parent
# 환경변수가 없으면 프로젝트 내부 경로로 fallback (Render 무료 티어에서도 작동).
# 영구 저장이 필요하면 Persistent Disk를 mount하고 환경변수로 경로를 덮어쓰기.
UPLOAD_DIR = Path(os.environ.get(
    "UPLOAD_DIR", BASE_DIR / "static" / "uploads"))
DB_PATH = Path(os.environ.get(
    "DB_PATH", BASE_DIR / "problems.db"))

DEFAULT_UNITS = [
    "수열", "극한", "미분", "적분",
    "확률", "통계", "지수로그", "삼각함수",
]
DIFFICULTIES = ["1", "2", "3", "4", "5"]
ALLOWED_EXT = {"png", "jpg", "jpeg", "gif", "webp"}

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get(
    "SECRET_KEY", "mathglab-dev-secret-CHANGE-ME")
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024  # 16 MB

UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH.parent.mkdir(parents=True, exist_ok=True)


@app.route("/uploads/<path:filename>")
def uploaded_file(filename):
    """Serve user-uploaded images from UPLOAD_DIR (env-configurable)."""
    return send_from_directory(UPLOAD_DIR, filename)


# ---------- DB ----------
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    con = sqlite3.connect(DB_PATH)
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS problems (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            title           TEXT NOT NULL,
            unit            TEXT NOT NULL,
            difficulty      TEXT NOT NULL,
            source          TEXT,
            tags            TEXT,
            answer          TEXT,
            problem_image   TEXT NOT NULL,
            solution_image  TEXT,
            created_at      TEXT NOT NULL
        )
        """
    )
    # units table: migrate from old (no parent_id) to new (with parent_id)
    units_info = con.execute("PRAGMA table_info(units)").fetchall()
    units_cols = {r[1] for r in units_info}
    if units_info and "parent_id" not in units_cols:
        existing = con.execute("SELECT id, name FROM units").fetchall()
        con.execute("DROP TABLE units")
        con.execute(
            """
            CREATE TABLE units (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                parent_id INTEGER REFERENCES units(id),
                name      TEXT NOT NULL
            )
            """
        )
        con.execute(
            "CREATE UNIQUE INDEX idx_unit_sibling "
            "ON units(COALESCE(parent_id, 0), name)"
        )
        for r in existing:
            con.execute(
                "INSERT INTO units (id, parent_id, name) VALUES (?, NULL, ?)",
                (r[0], r[1]),
            )
    elif not units_info:
        con.execute(
            """
            CREATE TABLE units (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                parent_id INTEGER REFERENCES units(id),
                name      TEXT NOT NULL
            )
            """
        )
        con.execute(
            "CREATE UNIQUE INDEX idx_unit_sibling "
            "ON units(COALESCE(parent_id, 0), name)"
        )
    # users table
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            username      TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            is_admin      INTEGER NOT NULL DEFAULT 0,
            created_at    TEXT NOT NULL
        )
        """
    )
    # Seed default admin if no users exist (race-safe under multiple workers)
    if con.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0:
        try:
            con.execute(
                "INSERT INTO users (username, password_hash, is_admin, created_at) "
                "VALUES (?, ?, 1, ?)",
                ("admin", generate_password_hash("admin"),
                 datetime.utcnow().isoformat(timespec="seconds")),
            )
            print("=" * 60)
            print(" 기본 관리자 계정 생성: admin / admin")
            print(" 로그인 후 /users 에서 비밀번호를 변경하세요.")
            print("=" * 60)
        except sqlite3.IntegrityError:
            pass  # another worker beat us to it
    admin_id = con.execute(
        "SELECT id FROM users WHERE is_admin = 1 ORDER BY id LIMIT 1"
    ).fetchone()[0]

    # settings table — migrate to per-user with composite PK(user_id, key)
    settings_info = con.execute("PRAGMA table_info(settings)").fetchall()
    settings_cols = {r[1] for r in settings_info}
    if settings_info and "user_id" not in settings_cols:
        rows = con.execute("SELECT key, value FROM settings").fetchall()
        con.execute("DROP TABLE settings")
        con.execute(
            """
            CREATE TABLE settings (
                user_id INTEGER NOT NULL,
                key     TEXT NOT NULL,
                value   TEXT,
                PRIMARY KEY (user_id, key)
            )
            """
        )
        for r in rows:
            con.execute(
                "INSERT INTO settings (user_id, key, value) VALUES (?, ?, ?)",
                (admin_id, r[0], r[1]),
            )
    elif not settings_info:
        con.execute(
            """
            CREATE TABLE settings (
                user_id INTEGER NOT NULL,
                key     TEXT NOT NULL,
                value   TEXT,
                PRIMARY KEY (user_id, key)
            )
            """
        )

    # exams table — add user_id
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS exams (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            title        TEXT NOT NULL,
            problem_ids  TEXT NOT NULL,
            options      TEXT,
            created_at   TEXT NOT NULL
        )
        """
    )
    exams_info = con.execute("PRAGMA table_info(exams)").fetchall()
    exams_cols = {r[1] for r in exams_info}
    if "user_id" not in exams_cols:
        con.execute("ALTER TABLE exams ADD COLUMN user_id INTEGER")
        con.execute("UPDATE exams SET user_id = ? WHERE user_id IS NULL",
                    (admin_id,))

    # Seed default units if empty
    if con.execute("SELECT COUNT(*) FROM units").fetchone()[0] == 0:
        con.executemany("INSERT INTO units (parent_id, name) VALUES (NULL, ?)",
                        [(u,) for u in DEFAULT_UNITS])
    # Migrate legacy difficulty values: 상→5, 중→3, 하→1
    con.execute("UPDATE problems SET difficulty='5' WHERE difficulty='상'")
    con.execute("UPDATE problems SET difficulty='3' WHERE difficulty='중'")
    con.execute("UPDATE problems SET difficulty='1' WHERE difficulty='하'")
    con.commit()
    con.close()


UNIT_PATH_SEP = " > "


def build_unit_tree() -> list[dict]:
    """Return list of root nodes; each node = {id, parent_id, name, path, children: [...]}."""
    rows = get_db().execute(
        "SELECT id, parent_id, name FROM units ORDER BY name"
    ).fetchall()
    nodes = {
        r["id"]: {
            "id": r["id"],
            "parent_id": r["parent_id"],
            "name": r["name"],
            "path": r["name"],  # filled in below for children
            "children": [],
        }
        for r in rows
    }
    roots: list[dict] = []
    for r in rows:
        node = nodes[r["id"]]
        if r["parent_id"] and r["parent_id"] in nodes:
            parent = nodes[r["parent_id"]]
            parent["children"].append(node)
        else:
            roots.append(node)

    def fill_paths(node: dict, prefix: str) -> None:
        node["path"] = f"{prefix}{UNIT_PATH_SEP}{node['name']}" if prefix else node["name"]
        for child in node["children"]:
            fill_paths(child, node["path"])

    for root in roots:
        fill_paths(root, "")
    return roots


def flatten_unit_tree(nodes: list[dict], depth: int = 0) -> list[dict]:
    """Flat list with depth, indented label, and full path — for dropdowns."""
    out: list[dict] = []
    for node in nodes:
        prefix = ("　" * depth) + ("└ " if depth > 0 else "")
        out.append({
            "id": node["id"],
            "depth": depth,
            "label": prefix + node["name"],
            "path": node["path"],
            "name": node["name"],
        })
        out.extend(flatten_unit_tree(node["children"], depth + 1))
    return out


def list_unit_paths() -> list[str]:
    """Valid unit values for problem.unit field."""
    return [u["path"] for u in flatten_unit_tree(build_unit_tree())]


def get_unit_options() -> list[dict]:
    """For <select> elements: [{path, label}, ...]"""
    return flatten_unit_tree(build_unit_tree())


def get_setting(key: str, user_id: int, default: str = "") -> str:
    row = get_db().execute(
        "SELECT value FROM settings WHERE user_id = ? AND key = ?",
        (user_id, key),
    ).fetchone()
    return row["value"] if row and row["value"] is not None else default


def set_setting(key: str, value: str | None, user_id: int) -> None:
    db = get_db()
    if value is None:
        db.execute("DELETE FROM settings WHERE user_id = ? AND key = ?",
                   (user_id, key))
    else:
        db.execute(
            "INSERT INTO settings (user_id, key, value) VALUES (?, ?, ?) "
            "ON CONFLICT(user_id, key) DO UPDATE SET value = excluded.value",
            (user_id, key, value),
        )
    db.commit()


# ---------- auth ----------
def current_user_id() -> int | None:
    return session.get("user_id")


def is_admin() -> bool:
    return bool(session.get("is_admin"))


def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not current_user_id():
            return redirect(url_for("login", next=request.path))
        return f(*args, **kwargs)
    return wrapper


def admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not current_user_id():
            return redirect(url_for("login", next=request.path))
        if not is_admin():
            abort(403)
        return f(*args, **kwargs)
    return wrapper


@app.context_processor
def inject_user():
    return {
        "current_user": {
            "id": session.get("user_id"),
            "username": session.get("username"),
            "is_admin": session.get("is_admin", False),
        }
    }


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        row = get_db().execute(
            "SELECT id, password_hash, is_admin FROM users WHERE username = ?",
            (username,),
        ).fetchone()
        if row and check_password_hash(row["password_hash"], password):
            session.clear()
            session["user_id"] = row["id"]
            session["username"] = username
            session["is_admin"] = bool(row["is_admin"])
            nxt = request.args.get("next") or url_for("search")
            return redirect(nxt)
        flash("아이디 또는 비밀번호가 올바르지 않습니다.", "error")
        return redirect(url_for("login"))
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------- helpers ----------
def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXT


def save_upload(file_storage) -> str | None:
    if not file_storage or not file_storage.filename:
        return None
    if not allowed_file(file_storage.filename):
        return None
    ext = file_storage.filename.rsplit(".", 1)[1].lower()
    name = f"{uuid.uuid4().hex}.{ext}"
    file_storage.save(UPLOAD_DIR / name)
    return name


def delete_upload(name: str | None) -> None:
    if not name:
        return
    try:
        (UPLOAD_DIR / name).unlink(missing_ok=True)
    except OSError:
        pass


def get_problem(pid: int) -> sqlite3.Row:
    row = get_db().execute("SELECT * FROM problems WHERE id = ?", (pid,)).fetchone()
    if row is None:
        abort(404)
    return row


# ---------- routes ----------
@app.route("/")
@login_required
def index():
    return redirect(url_for("search"))


# ----- problems: create -----
@app.route("/upload", methods=["GET", "POST"])
@admin_required
def upload():
    units = list_unit_paths()
    if request.method == "POST":
        ok, data, problem_name, solution_name = _parse_problem_form(units, edit=False)
        if not ok:
            return redirect(url_for("upload"))
        db = get_db()
        db.execute(
            """
            INSERT INTO problems
                (title, unit, difficulty, source, tags, answer,
                 problem_image, solution_image, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                data["title"], data["unit"], data["difficulty"],
                data["source"], data["tags"], data["answer"],
                problem_name, solution_name,
                datetime.utcnow().isoformat(timespec="seconds"),
            ),
        )
        db.commit()
        flash(f"'{data['title']}' 문제가 등록되었습니다.", "success")
        return redirect(url_for("upload"))

    return render_template("upload.html",
                           unit_options=get_unit_options(),
                           difficulties=DIFFICULTIES,
                           problem=None)


# ----- problems: edit -----
@app.route("/problem/<int:pid>/edit", methods=["GET", "POST"])
@admin_required
def edit_problem(pid):
    problem = get_problem(pid)
    units = list_unit_paths()

    if request.method == "POST":
        ok, data, new_problem_name, new_solution_name = _parse_problem_form(
            units, edit=True
        )
        if not ok:
            return redirect(url_for("edit_problem", pid=pid))

        # If a new problem image was uploaded, swap and delete old
        problem_image = problem["problem_image"]
        if new_problem_name:
            delete_upload(problem_image)
            problem_image = new_problem_name

        # Solution image: replace if uploaded, or remove if user asked
        solution_image = problem["solution_image"]
        if new_solution_name:
            delete_upload(solution_image)
            solution_image = new_solution_name
        elif request.form.get("remove_solution") == "1":
            delete_upload(solution_image)
            solution_image = None

        db = get_db()
        db.execute(
            """
            UPDATE problems
               SET title=?, unit=?, difficulty=?, source=?, tags=?, answer=?,
                   problem_image=?, solution_image=?
             WHERE id=?
            """,
            (
                data["title"], data["unit"], data["difficulty"],
                data["source"], data["tags"], data["answer"],
                problem_image, solution_image, pid,
            ),
        )
        db.commit()
        flash(f"'{data['title']}' 문제가 수정되었습니다.", "success")
        return redirect(url_for("search"))

    return render_template("upload.html",
                           unit_options=get_unit_options(),
                           difficulties=DIFFICULTIES,
                           problem=problem)


# ----- problems: delete -----
@app.post("/problem/<int:pid>/delete")
@admin_required
def delete_problem(pid):
    problem = get_problem(pid)
    delete_upload(problem["problem_image"])
    delete_upload(problem["solution_image"])
    db = get_db()
    db.execute("DELETE FROM problems WHERE id=?", (pid,))
    db.commit()
    flash(f"'{problem['title']}' 문제가 삭제되었습니다.", "success")
    return redirect(url_for("search"))


def _parse_problem_form(units, *, edit: bool):
    """Validate problem form. Returns (ok, data_dict, problem_name, solution_name).
    On failure, flashes error and returns (False, ...)."""
    title = request.form.get("title", "").strip()
    unit = request.form.get("unit", "").strip()
    difficulty = request.form.get("difficulty", "").strip()
    source = request.form.get("source", "").strip()
    tags = request.form.get("tags", "").strip()
    answer = request.form.get("answer", "").strip()
    problem_file = request.files.get("problem_image")
    solution_file = request.files.get("solution_image")

    if not title:
        flash("제목을 입력하세요.", "error"); return False, {}, None, None
    if unit not in units:
        flash("단원을 선택하세요.", "error"); return False, {}, None, None
    if difficulty not in DIFFICULTIES:
        flash("난이도를 선택하세요. (1~5)", "error"); return False, {}, None, None

    problem_name = save_upload(problem_file) if problem_file and problem_file.filename else None
    if not edit and not problem_name:
        flash("문제 이미지는 필수입니다.", "error"); return False, {}, None, None
    if problem_file and problem_file.filename and not problem_name:
        flash("문제 이미지 형식이 올바르지 않습니다 (png/jpg/jpeg/gif/webp).", "error")
        return False, {}, None, None

    solution_name = save_upload(solution_file) if solution_file and solution_file.filename else None
    if solution_file and solution_file.filename and not solution_name:
        flash("해설 이미지 형식이 올바르지 않습니다.", "error")
        return False, {}, None, None

    return True, {
        "title": title, "unit": unit, "difficulty": difficulty,
        "source": source, "tags": tags, "answer": answer,
    }, problem_name, solution_name


# ----- units: manage -----
@app.route("/units", methods=["GET"])
@admin_required
def units_page():
    tree = build_unit_tree()
    db = get_db()
    counts = dict(db.execute(
        "SELECT unit, COUNT(*) FROM problems GROUP BY unit"
    ).fetchall())
    return render_template("units.html", tree=tree, counts=counts)


@app.post("/units/add")
@admin_required
def units_add():
    name = request.form.get("name", "").strip()
    parent_id_raw = request.form.get("parent_id", "").strip()
    if not name:
        flash("단원 이름을 입력하세요.", "error")
        return redirect(url_for("units_page"))
    if len(name) > 30:
        flash("단원 이름은 30자 이내로 입력하세요.", "error")
        return redirect(url_for("units_page"))
    if UNIT_PATH_SEP.strip() in name:
        flash(f"단원 이름에는 '{UNIT_PATH_SEP.strip()}' 문자를 사용할 수 없습니다.", "error")
        return redirect(url_for("units_page"))

    parent_id = None
    parent_name = None
    if parent_id_raw:
        try:
            parent_id = int(parent_id_raw)
        except ValueError:
            abort(400)
        parent_row = get_db().execute(
            "SELECT name FROM units WHERE id=?", (parent_id,)
        ).fetchone()
        if parent_row is None:
            abort(404)
        parent_name = parent_row["name"]

    db = get_db()
    try:
        db.execute(
            "INSERT INTO units (parent_id, name) VALUES (?, ?)",
            (parent_id, name),
        )
        db.commit()
        if parent_name:
            flash(f"'{parent_name}' 안에 하위 단원 '{name}'을(를) 추가했습니다.", "success")
        else:
            flash(f"단원 '{name}'을(를) 추가했습니다.", "success")
    except sqlite3.IntegrityError:
        loc = f"'{parent_name}' 안" if parent_name else "최상위"
        flash(f"{loc}에 단원 '{name}'은(는) 이미 존재합니다.", "error")
    return redirect(url_for("units_page"))


@app.post("/units/<int:uid>/delete")
@admin_required
def units_delete(uid):
    db = get_db()
    row = db.execute("SELECT name FROM units WHERE id=?", (uid,)).fetchone()
    if row is None:
        abort(404)
    name = row["name"]

    # Block if has children
    child_count = db.execute(
        "SELECT COUNT(*) FROM units WHERE parent_id=?", (uid,)
    ).fetchone()[0]
    if child_count > 0:
        flash(f"단원 '{name}'에는 하위 단원이 {child_count}개 있어 삭제할 수 없습니다. "
              f"먼저 하위 단원을 모두 삭제하세요.", "error")
        return redirect(url_for("units_page"))

    # Block if used by problems (search by full path of this unit)
    paths_for_uid = [n["path"] for n in flatten_unit_tree(build_unit_tree())
                     if n["id"] == uid]
    if paths_for_uid:
        used = db.execute(
            "SELECT COUNT(*) FROM problems WHERE unit=?", (paths_for_uid[0],)
        ).fetchone()[0]
        if used > 0:
            flash(f"단원 '{paths_for_uid[0]}'은(는) {used}개 문제에서 사용 중이라 "
                  f"삭제할 수 없습니다.", "error")
            return redirect(url_for("units_page"))

    db.execute("DELETE FROM units WHERE id=?", (uid,))
    db.commit()
    flash(f"단원 '{name}'을(를) 삭제했습니다.", "success")
    return redirect(url_for("units_page"))


# ----- settings (per-user academy info) -----
@app.route("/settings", methods=["GET", "POST"])
@login_required
def settings_page():
    uid = current_user_id()
    if request.method == "POST":
        academy_text = request.form.get("academy_text", "").strip()
        set_setting("academy_text", academy_text, uid)

        logo_file = request.files.get("academy_logo")
        if logo_file and logo_file.filename:
            if not allowed_file(logo_file.filename):
                flash("로고 이미지 형식이 올바르지 않습니다.", "error")
                return redirect(url_for("settings_page"))
            new_name = save_upload(logo_file)
            old_name = get_setting("academy_logo", uid, "")
            if old_name:
                delete_upload(old_name)
            set_setting("academy_logo", new_name, uid)
        elif request.form.get("remove_logo") == "1":
            old_name = get_setting("academy_logo", uid, "")
            if old_name:
                delete_upload(old_name)
            set_setting("academy_logo", None, uid)

        flash("학원 정보가 저장되었습니다.", "success")
        return redirect(url_for("settings_page"))

    return render_template(
        "settings.html",
        academy_text=get_setting("academy_text", uid, ""),
        academy_logo=get_setting("academy_logo", uid, ""),
    )


# ----- search -----
@app.route("/search")
@login_required
def search():
    unit = request.args.get("unit", "").strip()
    difficulty = request.args.get("difficulty", "").strip()
    source = request.args.get("source", "").strip()
    tag = request.args.get("tag", "").strip()

    sql = "SELECT * FROM problems WHERE 1=1"
    params: list = []
    if unit:
        sql += " AND unit = ?"; params.append(unit)
    if difficulty:
        sql += " AND difficulty = ?"; params.append(difficulty)
    if source:
        sql += " AND source LIKE ?"; params.append(f"%{source}%")
    if tag:
        sql += " AND tags LIKE ?"; params.append(f"%{tag}%")
    sql += " ORDER BY id DESC"

    db = get_db()
    rows = db.execute(sql, params).fetchall()
    sources = [
        r["source"] for r in db.execute(
            "SELECT DISTINCT source FROM problems "
            "WHERE source IS NOT NULL AND source <> '' ORDER BY source"
        ).fetchall()
    ]
    return render_template(
        "search.html",
        problems=rows,
        unit_options=get_unit_options(),
        difficulties=DIFFICULTIES,
        sources=sources,
        filters={"unit": unit, "difficulty": difficulty, "source": source, "tag": tag},
    )


# ----- exam -----
PER_PAGE_OPTIONS = [0, 1, 2, 4, 6, 8, 10, 12]
DEFAULT_EXAM_OPTS = {
    "title": "시험지 제목을 입력하세요",
    "title_font": "",
    "title_size": "36px",
    "title_align": "left",
    "columns": 2,
    "gap": "8mm",
    "show_meta": True,
    "hide": [],
}


def _fetch_problems_in_order(ids: list[int]) -> list[sqlite3.Row]:
    if not ids:
        return []
    placeholders = ",".join("?" * len(ids))
    rows = get_db().execute(
        f"SELECT * FROM problems WHERE id IN ({placeholders})", ids
    ).fetchall()
    by_id = {r["id"]: r for r in rows}
    return [by_id[i] for i in ids if i in by_id]


def _chunk(problems: list, per_page: int) -> list[list]:
    if per_page > 0:
        return [problems[i:i + per_page] for i in range(0, len(problems), per_page)]
    return [problems] if problems else []


def _normalize_per_page(v) -> int:
    try:
        n = int(v)
    except (ValueError, TypeError):
        return 0
    return n if n in PER_PAGE_OPTIONS else 0


def _render_exam(problems, per_page, opts, saved_id=None):
    uid = current_user_id()
    return render_template(
        "exam.html",
        problems=problems,
        problem_pages=_chunk(problems, per_page),
        per_page=per_page,
        per_page_options=PER_PAGE_OPTIONS,
        today=datetime.now().strftime("%Y-%m-%d"),
        academy_text=get_setting("academy_text", uid, ""),
        academy_logo=get_setting("academy_logo", uid, ""),
        opts=opts,
        saved_id=saved_id,
    )


@app.route("/exam")
@login_required
def exam():
    raw_ids = request.args.getlist("ids")
    ids: list[int] = []
    for v in raw_ids:
        try:
            ids.append(int(v))
        except ValueError:
            continue

    problems = _fetch_problems_in_order(ids)
    per_page = _normalize_per_page(request.args.get("per_page", "0"))
    return _render_exam(problems, per_page, dict(DEFAULT_EXAM_OPTS))


# ----- saved exams (per-user) -----
@app.get("/exams")
@login_required
def exams_list():
    uid = current_user_id()
    rows = get_db().execute(
        "SELECT id, title, problem_ids, created_at FROM exams "
        "WHERE user_id = ? ORDER BY id DESC",
        (uid,),
    ).fetchall()
    items = []
    for r in rows:
        try:
            n = len(json.loads(r["problem_ids"]))
        except (TypeError, ValueError):
            n = 0
        items.append({
            "id": r["id"],
            "title": r["title"],
            "count": n,
            "created_at": r["created_at"],
        })
    return render_template("exams.html", items=items)


@app.post("/exams")
@login_required
def exams_save():
    data = request.get_json(silent=True) or {}
    title = (data.get("title") or "").strip()
    ids_raw = data.get("ids") or []
    if not title:
        return jsonify(error="제목이 비어있습니다."), 400
    try:
        ids = [int(i) for i in ids_raw]
    except (TypeError, ValueError):
        return jsonify(error="문제 ID가 올바르지 않습니다."), 400
    if not ids:
        return jsonify(error="문제가 비어있습니다."), 400

    opts = dict(DEFAULT_EXAM_OPTS)
    for k in opts.keys():
        if k in data and data[k] is not None:
            opts[k] = data[k]
    opts["per_page"] = _normalize_per_page(data.get("per_page", 0))

    db = get_db()
    cur = db.execute(
        "INSERT INTO exams (user_id, title, problem_ids, options, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            current_user_id(),
            title,
            json.dumps(ids),
            json.dumps(opts, ensure_ascii=False),
            datetime.utcnow().isoformat(timespec="seconds"),
        ),
    )
    db.commit()
    return jsonify(ok=True, id=cur.lastrowid, title=title)


def _get_owned_exam(eid: int) -> sqlite3.Row:
    """Return exam row if owned by current user (or admin), else 403/404."""
    row = get_db().execute(
        "SELECT * FROM exams WHERE id = ?", (eid,)
    ).fetchone()
    if row is None:
        abort(404)
    if row["user_id"] != current_user_id() and not is_admin():
        abort(403)
    return row


@app.get("/exams/<int:eid>")
@login_required
def exams_open(eid):
    row = _get_owned_exam(eid)

    try:
        ids = [int(i) for i in json.loads(row["problem_ids"])]
    except (TypeError, ValueError):
        ids = []
    try:
        saved = json.loads(row["options"] or "{}")
    except (TypeError, ValueError):
        saved = {}

    opts = dict(DEFAULT_EXAM_OPTS)
    opts.update({k: v for k, v in saved.items() if k in DEFAULT_EXAM_OPTS})
    opts["title"] = saved.get("title") or row["title"]

    per_page = _normalize_per_page(saved.get("per_page", 0))
    problems = _fetch_problems_in_order(ids)
    return _render_exam(problems, per_page, opts, saved_id=eid)


@app.post("/exams/<int:eid>/delete")
@login_required
def exams_delete(eid):
    row = _get_owned_exam(eid)
    db = get_db()
    db.execute("DELETE FROM exams WHERE id = ?", (eid,))
    db.commit()
    flash(f"'{row['title']}' 시험지를 보관함에서 삭제했습니다.", "success")
    return redirect(url_for("exams_list"))


# ----- user management (admin only) -----
@app.get("/users")
@admin_required
def users_list():
    rows = get_db().execute(
        "SELECT id, username, is_admin, created_at FROM users ORDER BY id"
    ).fetchall()
    return render_template("users.html", users=rows)


@app.post("/users/add")
@admin_required
def users_add():
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "").strip()
    make_admin = request.form.get("is_admin") == "1"
    if not username or not password:
        flash("아이디와 비밀번호를 모두 입력하세요.", "error")
        return redirect(url_for("users_list"))
    if len(username) > 30 or len(password) < 4:
        flash("아이디는 30자 이내, 비밀번호는 최소 4자 이상이어야 합니다.", "error")
        return redirect(url_for("users_list"))
    db = get_db()
    try:
        db.execute(
            "INSERT INTO users (username, password_hash, is_admin, created_at) "
            "VALUES (?, ?, ?, ?)",
            (
                username,
                generate_password_hash(password),
                1 if make_admin else 0,
                datetime.utcnow().isoformat(timespec="seconds"),
            ),
        )
        db.commit()
        flash(f"사용자 '{username}'을(를) 추가했습니다.", "success")
    except sqlite3.IntegrityError:
        flash(f"아이디 '{username}'은(는) 이미 존재합니다.", "error")
    return redirect(url_for("users_list"))


@app.post("/users/<int:uid>/password")
@admin_required
def users_change_password(uid):
    new_pw = request.form.get("password", "").strip()
    if not new_pw or len(new_pw) < 4:
        flash("새 비밀번호는 4자 이상이어야 합니다.", "error")
        return redirect(url_for("users_list"))
    db = get_db()
    row = db.execute("SELECT username FROM users WHERE id = ?", (uid,)).fetchone()
    if row is None:
        abort(404)
    db.execute(
        "UPDATE users SET password_hash = ? WHERE id = ?",
        (generate_password_hash(new_pw), uid),
    )
    db.commit()
    flash(f"'{row['username']}'의 비밀번호를 변경했습니다.", "success")
    return redirect(url_for("users_list"))


@app.post("/users/<int:uid>/delete")
@admin_required
def users_delete(uid):
    if uid == current_user_id():
        flash("현재 로그인한 본인 계정은 삭제할 수 없습니다.", "error")
        return redirect(url_for("users_list"))
    db = get_db()
    row = db.execute(
        "SELECT username, is_admin FROM users WHERE id = ?", (uid,)
    ).fetchone()
    if row is None:
        abort(404)
    # Block deleting the last admin
    if row["is_admin"]:
        admin_count = db.execute(
            "SELECT COUNT(*) FROM users WHERE is_admin = 1"
        ).fetchone()[0]
        if admin_count <= 1:
            flash("마지막 관리자 계정은 삭제할 수 없습니다.", "error")
            return redirect(url_for("users_list"))
    # Clean up the user's settings logo file
    logo = db.execute(
        "SELECT value FROM settings WHERE user_id = ? AND key = 'academy_logo'",
        (uid,),
    ).fetchone()
    if logo and logo["value"]:
        delete_upload(logo["value"])
    db.execute("DELETE FROM exams    WHERE user_id = ?", (uid,))
    db.execute("DELETE FROM settings WHERE user_id = ?", (uid,))
    db.execute("DELETE FROM users    WHERE id = ?", (uid,))
    db.commit()
    flash(f"사용자 '{row['username']}' 및 관련 데이터를 삭제했습니다.", "success")
    return redirect(url_for("users_list"))


# Run schema migration at import time so it works under both
# `python app.py` (dev) and `gunicorn app:app` (production).
init_db()


if __name__ == "__main__":
    debug = os.environ.get("FLASK_ENV", "development") != "production"
    port = int(os.environ.get("PORT", 5000))
    host = "127.0.0.1" if debug else "0.0.0.0"
    app.run(debug=debug, host=host, port=port)
