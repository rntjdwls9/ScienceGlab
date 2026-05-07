import json
import os
import re
import shutil
import sqlite3
import uuid
from datetime import datetime
from functools import wraps
from pathlib import Path

import fitz  # PyMuPDF
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
    "물리학", "화학", "생명과학", "지구과학",
    "물리학II", "화학II", "생명과학II", "지구과학II",
]
DIFFICULTIES = ["1", "2", "3", "4", "5"]
PROBLEM_TYPES = ["객관식", "주관식", "서술형"]
ALLOWED_EXT = {"png", "jpg", "jpeg", "gif", "webp"}
ALLOWED_ATTACH_EXT = ALLOWED_EXT | {
    "pdf", "doc", "docx", "ppt", "pptx", "xls", "xlsx",
    "hwp", "hwpx", "txt", "csv", "rtf", "md",
    "zip", "mp4", "mp3",
}

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get(
    "SECRET_KEY", "scienceglab-dev-secret-CHANGE-ME")
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024  # 16 MB

UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

# 일괄등록용 임시 작업 폴더 (PDF 원본 + 페이지 미리보기 PNG 보관)
BATCH_DIR = UPLOAD_DIR / "_batch"
BATCH_DIR.mkdir(parents=True, exist_ok=True)
PDF_PREVIEW_DPI = 150  # 화면 미리보기 해상도
PDF_CROP_DPI = 200     # 최종 저장 영역 해상도


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
    if "folder_id" not in exams_cols:
        con.execute("ALTER TABLE exams ADD COLUMN folder_id INTEGER")
    # 보관함 폴더 (per-user, 중첩 가능)
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS exam_folders (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER NOT NULL,
            parent_id   INTEGER REFERENCES exam_folders(id),
            name        TEXT NOT NULL,
            position    INTEGER NOT NULL DEFAULT 0,
            created_at  TEXT NOT NULL,
            is_template INTEGER NOT NULL DEFAULT 0,
            sort_order  INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    con.execute("CREATE INDEX IF NOT EXISTS idx_exam_folders_user "
                "ON exam_folders(user_id, COALESCE(parent_id, 0))")

    # concepts table — 시험지에 넣을 개념정리 자료
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS concepts (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            title       TEXT NOT NULL,
            unit        TEXT NOT NULL,
            source      TEXT,
            tags        TEXT,
            image       TEXT NOT NULL,
            created_at  TEXT NOT NULL
        )
        """
    )

    # posts table — 공지사항(notice) / 문의(inquiry) / 자료실(material) 통합
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS posts (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            board       TEXT NOT NULL CHECK(board IN ('notice','inquiry','material')),
            user_id     INTEGER NOT NULL,
            title       TEXT NOT NULL,
            body        TEXT,
            image       TEXT,
            created_at  TEXT NOT NULL
        )
        """
    )
    # Migrate older posts table whose CHECK constraint lacks 'material'
    posts_sql_row = con.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='posts'"
    ).fetchone()
    posts_sql = (posts_sql_row[0] if posts_sql_row else "") or ""
    if posts_sql and "'material'" not in posts_sql:
        con.execute("ALTER TABLE posts RENAME TO posts_old")
        con.execute(
            """
            CREATE TABLE posts (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                board       TEXT NOT NULL CHECK(board IN ('notice','inquiry','material')),
                user_id     INTEGER NOT NULL,
                title       TEXT NOT NULL,
                body        TEXT,
                image       TEXT,
                created_at  TEXT NOT NULL
            )
            """
        )
        con.execute(
            "INSERT INTO posts (id, board, user_id, title, body, image, created_at) "
            "SELECT id, board, user_id, title, body, image, created_at FROM posts_old"
        )
        con.execute("DROP TABLE posts_old")
    con.execute("CREATE INDEX IF NOT EXISTS idx_posts_board_id ON posts(board, id)")

    # post_attachments — 한 게시글에 여러 첨부파일
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS post_attachments (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            post_id       INTEGER NOT NULL REFERENCES posts(id),
            original_name TEXT NOT NULL,
            stored_name   TEXT NOT NULL,
            size          INTEGER NOT NULL DEFAULT 0,
            mime          TEXT,
            created_at    TEXT NOT NULL
        )
        """
    )
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_post_attachments_post "
        "ON post_attachments(post_id)"
    )

    # units: 정렬용 position 컬럼 마이그레이션
    units_info2 = con.execute("PRAGMA table_info(units)").fetchall()
    if units_info2 and "position" not in {r[1] for r in units_info2}:
        con.execute(
            "ALTER TABLE units ADD COLUMN position INTEGER NOT NULL DEFAULT 0"
        )
        # 기존 단원에 형제 그룹별로 이름순 position 부여
        rows = con.execute(
            "SELECT id, parent_id FROM units "
            "ORDER BY COALESCE(parent_id, 0), name"
        ).fetchall()
        next_pos: dict = {}
        for r in rows:
            key = r[1] if r[1] is not None else 0
            p = next_pos.get(key, 0)
            con.execute("UPDATE units SET position=? WHERE id=?", (p, r[0]))
            next_pos[key] = p + 1

    # Seed default units if empty
    if con.execute("SELECT COUNT(*) FROM units").fetchone()[0] == 0:
        con.executemany(
            "INSERT INTO units (parent_id, name, position) VALUES (NULL, ?, ?)",
            [(u, i) for i, u in enumerate(DEFAULT_UNITS)],
        )

    # learning videos board: 폴더 트리 + 영상 링크
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS learning_folders (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            parent_id  INTEGER REFERENCES learning_folders(id),
            name       TEXT NOT NULL,
            position   INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )
        """
    )
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_learning_folders_parent "
        "ON learning_folders(COALESCE(parent_id, 0), position)"
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS learning_videos (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            folder_id   INTEGER NOT NULL REFERENCES learning_folders(id),
            title       TEXT NOT NULL,
            url         TEXT NOT NULL,
            description TEXT,
            position    INTEGER NOT NULL DEFAULT 0,
            created_at  TEXT NOT NULL
        )
        """
    )
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_learning_videos_folder "
        "ON learning_videos(folder_id, position)"
    )
    if con.execute(
        "SELECT COUNT(*) FROM learning_folders WHERE parent_id IS NULL"
    ).fetchone()[0] == 0:
        _now = datetime.utcnow().isoformat(timespec="seconds")
        con.executemany(
            "INSERT INTO learning_folders "
            "(parent_id, name, position, created_at) "
            "VALUES (NULL, ?, ?, ?)",
            [("초등", 0, _now), ("중등", 1, _now), ("고등", 2, _now)],
        )

    # Migrate legacy difficulty values: 상→5, 중→3, 하→1
    con.execute("UPDATE problems SET difficulty='5' WHERE difficulty='상'")
    con.execute("UPDATE problems SET difficulty='3' WHERE difficulty='중'")
    con.execute("UPDATE problems SET difficulty='1' WHERE difficulty='하'")

    # Add problem_type column (객관식/주관식/서술형) — nullable for existing rows
    problems_cols = {r[1] for r in con.execute(
        "PRAGMA table_info(problems)").fetchall()}
    if "problem_type" not in problems_cols:
        con.execute("ALTER TABLE problems ADD COLUMN problem_type TEXT")

    con.commit()
    con.close()


UNIT_PATH_SEP = " > "


def build_unit_tree() -> list[dict]:
    """Return list of root nodes; each node = {id, parent_id, name, path, children: [...]}."""
    rows = get_db().execute(
        "SELECT id, parent_id, name FROM units ORDER BY position, name"
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


def _attach_ext_ok(filename: str) -> bool:
    return ("." in filename
            and filename.rsplit(".", 1)[1].lower() in ALLOWED_ATTACH_EXT)


def save_attachments(db, post_id: int, files) -> int:
    """Save uploaded files as attachments for a post. Returns count saved."""
    saved = 0
    now = datetime.utcnow().isoformat(timespec="seconds")
    for f in files or []:
        if not f or not f.filename:
            continue
        original = f.filename
        if not _attach_ext_ok(original):
            flash(f"허용되지 않는 파일 형식: {original}", "error")
            continue
        ext = original.rsplit(".", 1)[1].lower()
        stored = f"{uuid.uuid4().hex}.{ext}"
        dest = UPLOAD_DIR / stored
        f.save(dest)
        try:
            size = dest.stat().st_size
        except OSError:
            size = 0
        db.execute(
            "INSERT INTO post_attachments "
            "(post_id, original_name, stored_name, size, mime, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (post_id, original, stored, size, f.mimetype or None, now),
        )
        saved += 1
    return saved


def get_post_attachments(db, post_id: int) -> list:
    return db.execute(
        "SELECT id, original_name, stored_name, size, mime "
        "FROM post_attachments WHERE post_id=? ORDER BY id",
        (post_id,),
    ).fetchall()


def delete_post_attachments(db, post_id: int, only_ids: list | None = None) -> None:
    """Delete attachment files + rows. If only_ids is given, restrict to those."""
    if only_ids is not None:
        if not only_ids:
            return
        placeholders = ",".join("?" * len(only_ids))
        rows = db.execute(
            f"SELECT id, stored_name FROM post_attachments "
            f"WHERE post_id=? AND id IN ({placeholders})",
            [post_id, *only_ids],
        ).fetchall()
    else:
        rows = db.execute(
            "SELECT id, stored_name FROM post_attachments WHERE post_id=?",
            (post_id,),
        ).fetchall()
    for r in rows:
        delete_upload(r["stored_name"])
    if only_ids is not None:
        placeholders = ",".join("?" * len(only_ids))
        db.execute(
            f"DELETE FROM post_attachments "
            f"WHERE post_id=? AND id IN ({placeholders})",
            [post_id, *only_ids],
        )
    else:
        db.execute("DELETE FROM post_attachments WHERE post_id=?", (post_id,))


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
                (title, unit, difficulty, problem_type, source, tags, answer,
                 problem_image, solution_image, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                data["title"], data["unit"], data["difficulty"],
                data["problem_type"],
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
                           problem_types=PROBLEM_TYPES,
                           problem=None)


# ----- problems: PDF 일괄 등록 -----
def _is_session_id(s: str) -> bool:
    return isinstance(s, str) and len(s) == 32 and all(
        c in "0123456789abcdef" for c in s
    )


@app.get("/upload/batch")
@admin_required
def upload_batch():
    return render_template(
        "upload_batch.html",
        unit_options=get_unit_options(),
        difficulties=DIFFICULTIES,
        problem_types=PROBLEM_TYPES,
    )


def _render_pdf_pages(pdf_path: Path, sess_dir: Path,
                      session_id: str, kind: str) -> list[dict]:
    """Render a PDF's pages to PNG previews and return page metadata.

    kind ∈ {"main", "solution"} — used in filename prefix and returned dicts.
    """
    doc = fitz.open(pdf_path)
    pages: list[dict] = []
    zoom = PDF_PREVIEW_DPI / 72.0
    matrix = fitz.Matrix(zoom, zoom)
    try:
        for i, page in enumerate(doc):
            pix = page.get_pixmap(matrix=matrix, alpha=False)
            png_name = f"{kind}_page_{i}.png"
            pix.save(sess_dir / png_name)
            pages.append({
                "index": i,
                "kind": kind,  # 'main' | 'solution'
                "url": url_for(
                    "uploaded_file",
                    filename=f"_batch/{session_id}/{png_name}",
                ),
                "width": pix.width,
                "height": pix.height,
            })
    finally:
        doc.close()
    return pages


@app.post("/upload/batch/pdf")
@admin_required
def upload_batch_pdf():
    main_file = request.files.get("pdf")
    sol_file  = request.files.get("solution_pdf")  # 선택
    if not main_file or not main_file.filename:
        return jsonify({"error": "문제 PDF를 선택하세요."}), 400
    if not main_file.filename.lower().endswith(".pdf"):
        return jsonify({"error": "PDF 파일만 업로드 가능합니다."}), 400
    if sol_file and sol_file.filename and not sol_file.filename.lower().endswith(".pdf"):
        return jsonify({"error": "해설 파일도 PDF여야 합니다."}), 400

    session_id = uuid.uuid4().hex
    sess_dir = BATCH_DIR / session_id
    sess_dir.mkdir(parents=True, exist_ok=True)

    main_path = sess_dir / "main.pdf"
    main_file.save(main_path)

    sol_path: Path | None = None
    if sol_file and sol_file.filename:
        sol_path = sess_dir / "solution.pdf"
        sol_file.save(sol_path)

    try:
        main_pages = _render_pdf_pages(main_path, sess_dir, session_id, "main")
        sol_pages: list[dict] = []
        if sol_path is not None:
            sol_pages = _render_pdf_pages(sol_path, sess_dir, session_id, "solution")
    except Exception as e:
        shutil.rmtree(sess_dir, ignore_errors=True)
        return jsonify({"error": f"PDF를 처리할 수 없습니다: {e}"}), 400

    return jsonify({
        "session_id": session_id,
        "pages": main_pages + sol_pages,
        "filename": main_file.filename,
        "solution_filename": sol_file.filename if (sol_file and sol_file.filename) else None,
        "has_solution_pdf": sol_path is not None,
    })


def _crop_region(doc: "fitz.Document", page_idx: int,
                 x: float, y: float, w: float, h: float) -> str:
    """Render a normalized region from `doc[page_idx]` and save to UPLOAD_DIR.
    Returns saved filename."""
    if page_idx < 0 or page_idx >= doc.page_count:
        raise ValueError(f"잘못된 페이지 번호: {page_idx}")
    page = doc[page_idx]
    r = page.rect
    x = max(0.0, min(1.0, float(x)))
    y = max(0.0, min(1.0, float(y)))
    w = max(0.001, min(1.0 - x, float(w)))
    h = max(0.001, min(1.0 - y, float(h)))
    clip = fitz.Rect(
        r.x0 + x * r.width,
        r.y0 + y * r.height,
        r.x0 + (x + w) * r.width,
        r.y0 + (y + h) * r.height,
    )
    zoom = PDF_CROP_DPI / 72.0
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom),
                          clip=clip, alpha=False)
    name = f"{uuid.uuid4().hex}.png"
    pix.save(UPLOAD_DIR / name)
    return name


@app.post("/upload/batch/commit")
@admin_required
def upload_batch_commit():
    data = request.get_json(silent=True) or {}
    session_id = data.get("session_id", "")
    crops = data.get("crops") or []
    entity = data.get("entity", "problem")  # 'problem' | 'concept'

    if not _is_session_id(session_id):
        return jsonify({"error": "잘못된 세션입니다."}), 400
    if entity not in ("problem", "concept"):
        return jsonify({"error": "잘못된 등록 종류입니다."}), 400

    sess_dir = BATCH_DIR / session_id
    main_path = sess_dir / "main.pdf"
    sol_path  = sess_dir / "solution.pdf"
    if not main_path.exists():
        return jsonify({"error": "세션이 만료되었습니다. 다시 업로드하세요."}), 400
    if not crops:
        return jsonify({"error": "등록할 영역이 없습니다."}), 400

    main_doc = fitz.open(main_path)
    sol_doc  = fitz.open(sol_path) if sol_path.exists() else None
    db = get_db()
    saved_files: list[str] = []  # 실패 시 롤백

    def doc_for(pdf_kind: str):
        if pdf_kind == "solution":
            if sol_doc is None:
                raise ValueError("해설 PDF가 업로드되지 않았습니다.")
            return sol_doc
        return main_doc

    units = list_unit_paths()

    try:
        if entity == "problem":
            problems  = [c for c in crops if c.get("kind") == "problem"]
            solutions = {c["temp_id"]: c for c in crops if c.get("kind") == "solution"}
            if not problems:
                return jsonify({"error": "문제 영역이 1개 이상 필요합니다."}), 400
            for i, c in enumerate(problems):
                if not str(c.get("title", "")).strip():
                    return jsonify({"error": f"문제 #{i+1}: 제목을 입력하세요."}), 400
                if c.get("unit") not in units:
                    return jsonify({"error": f"문제 #{i+1}: 단원을 선택하세요."}), 400
                if c.get("difficulty") not in DIFFICULTIES:
                    return jsonify({"error": f"문제 #{i+1}: 난이도를 선택하세요."}), 400
                pt = (c.get("problem_type") or "").strip()
                if pt not in PROBLEM_TYPES:
                    return jsonify({"error": f"문제 #{i+1}: 유형을 선택하세요. (객관식/주관식/서술형)"}), 400
                sid = c.get("solution_temp_id")
                if sid is not None and sid not in solutions:
                    return jsonify({"error": f"문제 #{i+1}: 연결된 해설을 찾을 수 없습니다."}), 400

            for c in problems:
                p_doc = doc_for(c.get("pdf", "main"))
                problem_name = _crop_region(
                    p_doc, int(c["page"]),
                    c["x"], c["y"], c["w"], c["h"],
                )
                saved_files.append(problem_name)

                solution_name: str | None = None
                sid = c.get("solution_temp_id")
                if sid is not None:
                    s = solutions[sid]
                    s_doc = doc_for(s.get("pdf", "main"))
                    solution_name = _crop_region(
                        s_doc, int(s["page"]),
                        s["x"], s["y"], s["w"], s["h"],
                    )
                    saved_files.append(solution_name)

                db.execute(
                    """
                    INSERT INTO problems
                        (title, unit, difficulty, problem_type,
                         source, tags, answer,
                         problem_image, solution_image, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(c["title"]).strip(),
                        c["unit"],
                        c["difficulty"],
                        str(c.get("problem_type", "")).strip(),
                        str(c.get("source", "")).strip(),
                        str(c.get("tags", "")).strip(),
                        str(c.get("answer", "")).strip(),
                        problem_name, solution_name,
                        datetime.utcnow().isoformat(timespec="seconds"),
                    ),
                )
            count = len(problems)
            label = "문제"
        else:  # concept
            concepts = [c for c in crops if c.get("kind") == "concept"]
            if not concepts:
                return jsonify({"error": "개념 영역이 1개 이상 필요합니다."}), 400
            for i, c in enumerate(concepts):
                if not str(c.get("title", "")).strip():
                    return jsonify({"error": f"개념 #{i+1}: 제목을 입력하세요."}), 400
                if c.get("unit") not in units:
                    return jsonify({"error": f"개념 #{i+1}: 단원을 선택하세요."}), 400
            for c in concepts:
                p_doc = doc_for(c.get("pdf", "main"))
                image_name = _crop_region(
                    p_doc, int(c["page"]),
                    c["x"], c["y"], c["w"], c["h"],
                )
                saved_files.append(image_name)
                db.execute(
                    """
                    INSERT INTO concepts
                        (title, unit, source, tags, image, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(c["title"]).strip(),
                        c["unit"],
                        str(c.get("source", "")).strip(),
                        str(c.get("tags", "")).strip(),
                        image_name,
                        datetime.utcnow().isoformat(timespec="seconds"),
                    ),
                )
            count = len(concepts)
            label = "개념"

        db.commit()
    except Exception as e:
        db.rollback()
        for name in saved_files:
            delete_upload(name)
        main_doc.close()
        if sol_doc: sol_doc.close()
        return jsonify({"error": f"등록 중 오류: {e}"}), 500
    else:
        main_doc.close()
        if sol_doc: sol_doc.close()
        shutil.rmtree(sess_dir, ignore_errors=True)
        flash(f"{count}개의 {label}이 등록되었습니다.", "success")
        return jsonify({"ok": True, "count": count,
                        "redirect": url_for("search")})


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
               SET title=?, unit=?, difficulty=?, problem_type=?,
                   source=?, tags=?, answer=?,
                   problem_image=?, solution_image=?
             WHERE id=?
            """,
            (
                data["title"], data["unit"], data["difficulty"],
                data["problem_type"],
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
                           problem_types=PROBLEM_TYPES,
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


# ----- concepts: delete -----
@app.post("/concept/<int:cid>/delete")
@admin_required
def delete_concept(cid):
    db = get_db()
    row = db.execute("SELECT * FROM concepts WHERE id=?", (cid,)).fetchone()
    if row is None:
        abort(404)
    delete_upload(row["image"])
    db.execute("DELETE FROM concepts WHERE id=?", (cid,))
    db.commit()
    flash(f"'{row['title']}' 개념이 삭제되었습니다.", "success")
    return redirect(url_for("search", tab="concept"))


def _parse_problem_form(units, *, edit: bool):
    """Validate problem form. Returns (ok, data_dict, problem_name, solution_name).
    On failure, flashes error and returns (False, ...)."""
    title = request.form.get("title", "").strip()
    unit = request.form.get("unit", "").strip()
    difficulty = request.form.get("difficulty", "").strip()
    problem_type = request.form.get("problem_type", "").strip()
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
    if problem_type not in PROBLEM_TYPES:
        flash("유형을 선택하세요. (객관식/주관식/서술형)", "error")
        return False, {}, None, None

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
        "problem_type": problem_type,
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
    next_pos = db.execute(
        "SELECT COALESCE(MAX(position), -1) + 1 FROM units "
        "WHERE COALESCE(parent_id, 0) = COALESCE(?, 0)",
        (parent_id,),
    ).fetchone()[0]
    try:
        db.execute(
            "INSERT INTO units (parent_id, name, position) VALUES (?, ?, ?)",
            (parent_id, name, next_pos),
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


@app.post("/units/<int:uid>/reorder")
@admin_required
def units_reorder(uid):
    """드래그앤드롭용. 같은 부모 안에서 anchor_uid 기준 before/after 로 이동."""
    pos_kind = request.form.get("position", "before")
    if pos_kind not in ("before", "after"):
        return jsonify({"error": "잘못된 position"}), 400
    try:
        anchor_uid = int(request.form.get("anchor_uid", ""))
    except ValueError:
        return jsonify({"error": "잘못된 anchor_uid"}), 400

    db = get_db()
    moved = db.execute(
        "SELECT id, parent_id FROM units WHERE id=?", (uid,)
    ).fetchone()
    target = db.execute(
        "SELECT id, parent_id FROM units WHERE id=?", (anchor_uid,)
    ).fetchone()
    if moved is None or target is None:
        return jsonify({"error": "단원을 찾을 수 없습니다."}), 404
    if (moved["parent_id"] or 0) != (target["parent_id"] or 0):
        return jsonify({"error": "다른 부모 단원으로는 이동할 수 없습니다."}), 400
    if uid == anchor_uid:
        return jsonify({"ok": True})

    parent_id = moved["parent_id"]
    rows = db.execute(
        "SELECT id FROM units "
        "WHERE COALESCE(parent_id, 0) = COALESCE(?, 0) "
        "ORDER BY position, name",
        (parent_id,),
    ).fetchall()
    ids = [r["id"] for r in rows]
    ids.remove(uid)
    target_idx = ids.index(anchor_uid)
    if pos_kind == "after":
        target_idx += 1
    ids.insert(target_idx, uid)

    for new_pos, _id in enumerate(ids):
        db.execute("UPDATE units SET position=? WHERE id=?", (new_pos, _id))
    db.commit()
    return jsonify({"ok": True})


@app.post("/units/<int:uid>/move")
@admin_required
def units_move(uid):
    direction = request.form.get("direction", "")
    if direction not in ("up", "down"):
        abort(400)
    db = get_db()
    row = db.execute(
        "SELECT id, parent_id, position FROM units WHERE id=?", (uid,)
    ).fetchone()
    if row is None:
        abort(404)

    if direction == "up":
        sib = db.execute(
            "SELECT id, position FROM units "
            "WHERE COALESCE(parent_id, 0) = COALESCE(?, 0) AND position < ? "
            "ORDER BY position DESC LIMIT 1",
            (row["parent_id"], row["position"]),
        ).fetchone()
    else:
        sib = db.execute(
            "SELECT id, position FROM units "
            "WHERE COALESCE(parent_id, 0) = COALESCE(?, 0) AND position > ? "
            "ORDER BY position ASC LIMIT 1",
            (row["parent_id"], row["position"]),
        ).fetchone()

    if sib is not None:
        db.execute("UPDATE units SET position=? WHERE id=?",
                   (sib["position"], row["id"]))
        db.execute("UPDATE units SET position=? WHERE id=?",
                   (row["position"], sib["id"]))
        db.commit()
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

    # Block if used by problems or concepts (search by full path of this unit)
    paths_for_uid = [n["path"] for n in flatten_unit_tree(build_unit_tree())
                     if n["id"] == uid]
    if paths_for_uid:
        path = paths_for_uid[0]
        used_p = db.execute(
            "SELECT COUNT(*) FROM problems WHERE unit=?", (path,)
        ).fetchone()[0]
        used_c = db.execute(
            "SELECT COUNT(*) FROM concepts WHERE unit=?", (path,)
        ).fetchone()[0]
        if used_p + used_c > 0:
            flash(f"단원 '{path}'은(는) 문제 {used_p}개 · 개념 {used_c}개에서 "
                  f"사용 중이라 삭제할 수 없습니다.", "error")
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
def _distinct_tags(db, table: str) -> list[str]:
    rows = db.execute(
        f"SELECT DISTINCT tags FROM {table} "
        f"WHERE tags IS NOT NULL AND tags <> ''"
    ).fetchall()
    bag: set = set()
    for r in rows:
        for t in (r["tags"] or "").split(","):
            t = t.strip()
            if t:
                bag.add(t)
    return sorted(bag)


@app.route("/search")
@login_required
def search():
    units = [u.strip() for u in request.args.getlist("unit") if u.strip()]
    difficulty = request.args.get("difficulty", "").strip()
    problem_type = request.args.get("problem_type", "").strip()
    source = request.args.get("source", "").strip()
    tag_raw = request.args.get("tag", "").strip()
    tags = [t.strip() for t in tag_raw.split(",") if t.strip()]

    tab = request.args.get("tab", "problem")
    if tab not in ("problem", "concept"):
        tab = "problem"

    table = "concepts" if tab == "concept" else "problems"

    conds = ["1=1"]
    params: list = []
    if units:
        parts = []
        for up in units:
            parts.append("(unit = ? OR unit LIKE ?)")
            params.extend([up, f"{up}{UNIT_PATH_SEP}%"])
        conds.append("(" + " OR ".join(parts) + ")")
    if tab == "problem" and difficulty:
        conds.append("difficulty = ?")
        params.append(difficulty)
    if tab == "problem" and problem_type:
        if problem_type == "__none__":
            conds.append("(problem_type IS NULL OR problem_type = '')")
        elif problem_type in PROBLEM_TYPES:
            conds.append("problem_type = ?")
            params.append(problem_type)
    if source:
        conds.append("source LIKE ?")
        params.append(f"%{source}%")
    for tg in tags:
        conds.append("tags LIKE ?")
        params.append(f"%{tg}%")

    db = get_db()
    sql = f"SELECT * FROM {table} WHERE " + " AND ".join(conds) + " ORDER BY id DESC"
    rows = db.execute(sql, params).fetchall()

    sources = [
        r["source"] for r in db.execute(
            f"SELECT DISTINCT source FROM {table} "
            "WHERE source IS NOT NULL AND source <> '' ORDER BY source"
        ).fetchall()
    ]
    tag_options = _distinct_tags(db, table)

    return render_template(
        "search.html",
        problems=rows,
        tab=tab,
        unit_options=get_unit_options(),
        difficulties=DIFFICULTIES,
        problem_types=PROBLEM_TYPES,
        sources=sources,
        tag_options=tag_options,
        filters={
            "units": units,
            "difficulty": difficulty,
            "problem_type": problem_type,
            "source": source,
            "tag": tag_raw,
            "tags": tags,
        },
    )


# ----- exam -----
PER_PAGE_OPTIONS = [0, 1, 2, 4, 6, 8, 10, 12]
DEFAULT_EXAM_OPTS = {
    "title": "시험지 제목을 입력하세요",
    "title_font": "",
    "title_size": "36px",
    "title_align": "left",
    "columns": 2,
    "gap": "40mm",
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


def _fetch_concepts_in_order(ids: list[int]) -> list[sqlite3.Row]:
    if not ids:
        return []
    placeholders = ",".join("?" * len(ids))
    rows = get_db().execute(
        f"SELECT * FROM concepts WHERE id IN ({placeholders})", ids
    ).fetchall()
    by_id = {r["id"]: r for r in rows}
    return [by_id[i] for i in ids if i in by_id]


def _render_exam(problems, per_page, opts, saved_id=None, concepts=None):
    uid = current_user_id()
    return render_template(
        "exam.html",
        problems=problems,
        problem_pages=_chunk(problems, per_page),
        concepts=concepts or [],
        per_page=per_page,
        per_page_options=PER_PAGE_OPTIONS,
        today=datetime.now().strftime("%Y-%m-%d"),
        academy_text=get_setting("academy_text", uid, ""),
        academy_logo=get_setting("academy_logo", uid, ""),
        opts=opts,
        saved_id=saved_id,
    )


def _parse_id_list(values: list[str]) -> list[int]:
    out: list[int] = []
    for v in values:
        try:
            out.append(int(v))
        except ValueError:
            continue
    return out


@app.route("/exam")
@login_required
def exam():
    ids         = _parse_id_list(request.args.getlist("ids"))
    concept_ids = _parse_id_list(request.args.getlist("concept_ids"))

    problems = _fetch_problems_in_order(ids)
    concepts = _fetch_concepts_in_order(concept_ids)
    per_page = _normalize_per_page(request.args.get("per_page", "0"))
    return _render_exam(problems, per_page, dict(DEFAULT_EXAM_OPTS),
                        concepts=concepts)


# ----- saved exams (per-user) with folders -----
def _exam_count(row) -> int:
    try:
        return len(json.loads(row["problem_ids"]))
    except (TypeError, ValueError):
        return 0


def _get_owned_folder(fid: int) -> sqlite3.Row | None:
    """Return folder row if owned by current user. None for fid=0/None (root).
    Aborts if fid given but not owned."""
    if not fid:
        return None
    row = get_db().execute(
        "SELECT * FROM exam_folders WHERE id=?", (fid,)
    ).fetchone()
    if row is None:
        abort(404)
    if row["user_id"] != current_user_id() and not is_admin():
        abort(403)
    return row


def _folder_breadcrumb(fid: int | None) -> list[dict]:
    """Return [root, ..., current] folder chain. root entry has id=None."""
    chain: list[dict] = []
    cur_id = fid
    db = get_db()
    while cur_id:
        row = db.execute(
            "SELECT id, parent_id, name FROM exam_folders WHERE id=?", (cur_id,)
        ).fetchone()
        if row is None:
            break
        chain.append({"id": row["id"], "name": row["name"]})
        cur_id = row["parent_id"]
    chain.reverse()
    return [{"id": None, "name": "보관함"}] + chain


def _copy_template_folders(conn, target_user_id: int) -> int:
    """admin의 is_template=1 폴더 트리를 target_user에게 복사. 빈 폴더만.

    같은 conn에서 INSERT만 수행 — commit/rollback은 호출자 책임.
    Returns: 복사된 폴더 개수 (admin 또는 템플릿 폴더가 없으면 0).
    """
    admin = conn.execute(
        "SELECT id FROM users WHERE is_admin = 1 ORDER BY id LIMIT 1"
    ).fetchone()
    if not admin:
        return 0
    admin_id = admin["id"]

    rows = conn.execute(
        "SELECT id, parent_id, name, position, sort_order "
        "FROM exam_folders "
        "WHERE user_id = ? AND is_template = 1",
        (admin_id,),
    ).fetchall()
    if not rows:
        return 0

    template_ids = {r["id"] for r in rows}
    by_parent: dict = {}
    for r in rows:
        pid = r["parent_id"]
        # 템플릿 트리 밖의 부모를 가리키면 새 트리에서 root로 승격
        if pid is not None and pid not in template_ids:
            pid = None
        by_parent.setdefault(pid, []).append(r)

    now = datetime.utcnow().isoformat(timespec="seconds")
    id_map: dict = {}
    queue = list(by_parent.get(None, []))
    count = 0
    while queue:
        r = queue.pop(0)
        old_id = r["id"]
        old_parent = r["parent_id"]
        if old_parent is not None and old_parent not in template_ids:
            old_parent = None
        new_parent = id_map.get(old_parent) if old_parent is not None else None
        cur = conn.execute(
            "INSERT INTO exam_folders "
            "(user_id, parent_id, name, position, created_at, "
            " is_template, sort_order) "
            "VALUES (?, ?, ?, ?, ?, 0, ?)",
            (target_user_id, new_parent, r["name"], r["position"], now,
             r["sort_order"]),
        )
        id_map[old_id] = cur.lastrowid
        count += 1
        queue.extend(by_parent.get(old_id, []))
    return count


@app.get("/exams")
@login_required
def exams_list():
    uid = current_user_id()
    q = (request.args.get("q") or "").strip()
    folder_id_raw = request.args.get("folder_id", "")
    folder_id: int | None = None
    if folder_id_raw:
        try:
            folder_id = int(folder_id_raw)
        except ValueError:
            folder_id = None

    db = get_db()
    if folder_id is not None:
        _get_owned_folder(folder_id)

    if q:
        exams = db.execute(
            "SELECT id, title, problem_ids, created_at, folder_id "
            "FROM exams WHERE user_id=? AND title LIKE ? "
            "ORDER BY id DESC",
            (uid, f"%{q}%"),
        ).fetchall()
        folder_path_cache: dict[int, str] = {}

        def folder_path(fid: int | None) -> str:
            if not fid:
                return "보관함"
            if fid in folder_path_cache:
                return folder_path_cache[fid]
            chain = []
            cur = fid
            while cur:
                r = db.execute(
                    "SELECT id, parent_id, name FROM exam_folders WHERE id=?",
                    (cur,),
                ).fetchone()
                if r is None:
                    break
                chain.append(r["name"])
                cur = r["parent_id"]
            label = "보관함 > " + " > ".join(reversed(chain))
            folder_path_cache[fid] = label
            return label

        items = [{
            "id": r["id"],
            "title": r["title"],
            "count": _exam_count(r),
            "created_at": r["created_at"],
            "folder_id": r["folder_id"],
            "folder_label": folder_path(r["folder_id"]),
        } for r in exams]
        subfolders = []
        breadcrumb = [{"id": None, "name": "보관함"}]
    else:
        if folder_id is None:
            subfolders = db.execute(
                "SELECT id, name, is_template FROM exam_folders "
                "WHERE user_id=? AND parent_id IS NULL "
                "ORDER BY position, id",
                (uid,),
            ).fetchall()
            exam_rows = db.execute(
                "SELECT id, title, problem_ids, created_at, folder_id "
                "FROM exams WHERE user_id=? AND folder_id IS NULL "
                "ORDER BY id DESC",
                (uid,),
            ).fetchall()
        else:
            subfolders = db.execute(
                "SELECT id, name, is_template FROM exam_folders "
                "WHERE user_id=? AND parent_id=? "
                "ORDER BY position, id",
                (uid, folder_id),
            ).fetchall()
            exam_rows = db.execute(
                "SELECT id, title, problem_ids, created_at, folder_id "
                "FROM exams WHERE user_id=? AND folder_id=? "
                "ORDER BY id DESC",
                (uid, folder_id),
            ).fetchall()
        items = [{
            "id": r["id"],
            "title": r["title"],
            "count": _exam_count(r),
            "created_at": r["created_at"],
            "folder_id": r["folder_id"],
        } for r in exam_rows]
        breadcrumb = _folder_breadcrumb(folder_id)

    all_folder_rows = db.execute(
        "SELECT id, parent_id, name FROM exam_folders "
        "WHERE user_id=? ORDER BY parent_id, position, id",
        (uid,),
    ).fetchall()
    folder_by_id = {r["id"]: dict(r) for r in all_folder_rows}

    def label_of(fid: int) -> str:
        parts = []
        cur = fid
        while cur:
            r = folder_by_id.get(cur)
            if not r:
                break
            parts.append(r["name"])
            cur = r["parent_id"]
        return " > ".join(reversed(parts))

    all_folders = [{"id": fid, "label": label_of(fid)}
                   for fid in folder_by_id]

    return render_template(
        "exams.html",
        items=items,
        subfolders=[{"id": r["id"], "name": r["name"],
                     "is_template": r["is_template"]} for r in subfolders],
        breadcrumb=breadcrumb,
        current_folder_id=folder_id,
        all_folders=all_folders,
        q=q,
    )


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

    folder_id_raw = data.get("folder_id")
    folder_id: int | None = None
    if folder_id_raw:
        try:
            folder_id = int(folder_id_raw)
        except (TypeError, ValueError):
            return jsonify(error="폴더가 올바르지 않습니다."), 400
        _get_owned_folder(folder_id)

    opts = dict(DEFAULT_EXAM_OPTS)
    for k in opts.keys():
        if k in data and data[k] is not None:
            opts[k] = data[k]
    opts["per_page"] = _normalize_per_page(data.get("per_page", 0))

    db = get_db()
    cur = db.execute(
        "INSERT INTO exams (user_id, title, problem_ids, options, created_at, folder_id) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            current_user_id(),
            title,
            json.dumps(ids),
            json.dumps(opts, ensure_ascii=False),
            datetime.utcnow().isoformat(timespec="seconds"),
            folder_id,
        ),
    )
    db.commit()
    return jsonify(ok=True, id=cur.lastrowid, title=title)


# ----- exam folders -----
@app.post("/exam_folders")
@login_required
def exam_folders_create():
    name = (request.form.get("name") or "").strip()
    if not name:
        flash("폴더 이름을 입력하세요.", "error")
        return redirect(request.referrer or url_for("exams_list"))
    if len(name) > 60:
        flash("폴더 이름은 60자 이내로 입력하세요.", "error")
        return redirect(request.referrer or url_for("exams_list"))
    parent_raw = request.form.get("parent_id", "")
    parent_id: int | None = None
    if parent_raw:
        try:
            parent_id = int(parent_raw)
        except ValueError:
            abort(400)
        _get_owned_folder(parent_id)

    # is_template: admin만 지정 가능 (일반 사용자가 폼 조작해도 무시)
    is_template = 1 if (is_admin() and request.form.get("is_template") == "1") else 0

    db = get_db()
    next_pos = db.execute(
        "SELECT COALESCE(MAX(position), -1) + 1 FROM exam_folders "
        "WHERE user_id=? AND COALESCE(parent_id, 0) = COALESCE(?, 0)",
        (current_user_id(), parent_id),
    ).fetchone()[0]
    db.execute(
        "INSERT INTO exam_folders "
        "(user_id, parent_id, name, position, created_at, is_template) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (current_user_id(), parent_id, name, next_pos,
         datetime.utcnow().isoformat(timespec="seconds"), is_template),
    )
    db.commit()
    msg = f"폴더 '{name}' 을 만들었습니다."
    if is_template:
        msg += " (신규 가입자에게 자동 제공)"
    flash(msg, "success")
    return redirect(url_for("exams_list", folder_id=parent_id) if parent_id
                    else url_for("exams_list"))


@app.post("/exam_folders/<int:fid>/rename")
@login_required
def exam_folders_rename(fid):
    folder = _get_owned_folder(fid)
    name = (request.form.get("name") or "").strip()
    if not name:
        flash("폴더 이름을 입력하세요.", "error")
    elif len(name) > 60:
        flash("폴더 이름은 60자 이내로 입력하세요.", "error")
    else:
        db = get_db()
        db.execute("UPDATE exam_folders SET name=? WHERE id=?", (name, fid))
        db.commit()
        flash("폴더 이름을 변경했습니다.", "success")
    return redirect(url_for("exams_list",
                            folder_id=folder["parent_id"]) if folder["parent_id"]
                    else url_for("exams_list"))


@app.post("/exam_folders/<int:fid>/delete")
@login_required
def exam_folders_delete(fid):
    folder = _get_owned_folder(fid)
    db = get_db()
    child_cnt = db.execute(
        "SELECT COUNT(*) FROM exam_folders WHERE parent_id=?", (fid,)
    ).fetchone()[0]
    exam_cnt = db.execute(
        "SELECT COUNT(*) FROM exams WHERE folder_id=?", (fid,)
    ).fetchone()[0]
    if child_cnt + exam_cnt > 0:
        flash(f"폴더 '{folder['name']}' 안에 하위 폴더 {child_cnt}개 · "
              f"시험지 {exam_cnt}개가 있어 삭제할 수 없습니다.", "error")
    else:
        db.execute("DELETE FROM exam_folders WHERE id=?", (fid,))
        db.commit()
        flash(f"폴더 '{folder['name']}' 을 삭제했습니다.", "success")
    return redirect(url_for("exams_list",
                            folder_id=folder["parent_id"]) if folder["parent_id"]
                    else url_for("exams_list"))


@app.post("/exams/<int:eid>/move")
@login_required
def exams_move(eid):
    row = _get_owned_exam(eid)
    target_raw = request.form.get("folder_id", "")
    target_id: int | None = None
    if target_raw:
        try:
            target_id = int(target_raw)
        except ValueError:
            abort(400)
        _get_owned_folder(target_id)
    db = get_db()
    db.execute("UPDATE exams SET folder_id=? WHERE id=?", (target_id, eid))
    db.commit()
    return redirect(request.referrer or url_for("exams_list"))


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


# ----- posts: 공지사항(notice) / 문의(inquiry) -----
def _get_post(pid: int, board: str) -> sqlite3.Row:
    row = get_db().execute(
        "SELECT p.*, u.username AS author "
        "FROM posts p LEFT JOIN users u ON u.id = p.user_id "
        "WHERE p.id = ? AND p.board = ?",
        (pid, board),
    ).fetchone()
    if row is None:
        abort(404)
    return row


def _parse_post_form(*, edit_image: str | None = None):
    """Parse title/body/image. Returns (ok, title, body, image_name, remove_image)."""
    title = request.form.get("title", "").strip()
    body = request.form.get("body", "").strip()
    image_file = request.files.get("image")
    remove_image = request.form.get("remove_image") == "1"

    if not title:
        flash("제목을 입력하세요.", "error")
        return False, None, None, None, False
    if len(title) > 200:
        flash("제목은 200자 이내로 입력하세요.", "error")
        return False, None, None, None, False

    image_name = None
    if image_file and image_file.filename:
        if not allowed_file(image_file.filename):
            flash("이미지 형식이 올바르지 않습니다 (png/jpg/jpeg/gif/webp).", "error")
            return False, None, None, None, False
        image_name = save_upload(image_file)

    return True, title, body, image_name, remove_image


# --- 공지사항 (notice): admin write, all users read ---
@app.get("/notices")
@login_required
def notices_list():
    rows = get_db().execute(
        "SELECT p.id, p.title, p.created_at, u.username AS author "
        "FROM posts p LEFT JOIN users u ON u.id = p.user_id "
        "WHERE p.board = 'notice' ORDER BY p.id DESC"
    ).fetchall()
    return render_template("notices.html", items=rows)


@app.get("/notices/new")
@admin_required
def notices_new():
    return render_template("notice_form.html", post=None)


@app.post("/notices")
@admin_required
def notices_create():
    ok, title, body, image_name, _ = _parse_post_form()
    if not ok:
        return redirect(url_for("notices_new"))
    db = get_db()
    cur = db.execute(
        "INSERT INTO posts (board, user_id, title, body, image, created_at) "
        "VALUES ('notice', ?, ?, ?, ?, ?)",
        (current_user_id(), title, body, image_name,
         datetime.utcnow().isoformat(timespec="seconds")),
    )
    save_attachments(db, cur.lastrowid, request.files.getlist("attachments"))
    db.commit()
    flash("공지사항이 등록되었습니다.", "success")
    return redirect(url_for("notices_list"))


@app.get("/notices/<int:pid>")
@login_required
def notices_view(pid):
    post = _get_post(pid, "notice")
    attachments = get_post_attachments(get_db(), pid)
    return render_template("notice_view.html", post=post, attachments=attachments)


@app.get("/notices/<int:pid>/edit")
@admin_required
def notices_edit_form(pid):
    post = _get_post(pid, "notice")
    attachments = get_post_attachments(get_db(), pid)
    return render_template("notice_form.html", post=post, attachments=attachments)


@app.post("/notices/<int:pid>/edit")
@admin_required
def notices_edit(pid):
    post = _get_post(pid, "notice")
    ok, title, body, new_image, remove_image = _parse_post_form()
    if not ok:
        return redirect(url_for("notices_edit_form", pid=pid))
    image = post["image"]
    if new_image:
        delete_upload(image)
        image = new_image
    elif remove_image:
        delete_upload(image)
        image = None
    db = get_db()
    db.execute(
        "UPDATE posts SET title=?, body=?, image=? WHERE id=?",
        (title, body, image, pid),
    )
    remove_aids = [int(x) for x in request.form.getlist("remove_attach") if x.isdigit()]
    if remove_aids:
        delete_post_attachments(db, pid, only_ids=remove_aids)
    save_attachments(db, pid, request.files.getlist("attachments"))
    db.commit()
    flash("공지사항이 수정되었습니다.", "success")
    return redirect(url_for("notices_view", pid=pid))


@app.post("/notices/<int:pid>/delete")
@admin_required
def notices_delete(pid):
    post = _get_post(pid, "notice")
    delete_upload(post["image"])
    db = get_db()
    delete_post_attachments(db, pid)
    db.execute("DELETE FROM posts WHERE id=?", (pid,))
    db.commit()
    flash("공지사항을 삭제했습니다.", "success")
    return redirect(url_for("notices_list"))


# --- 문의/건의 (inquiry): user writes, admin reads all (user sees own only) ---
@app.get("/inquiries")
@login_required
def inquiries_list():
    db = get_db()
    if is_admin():
        rows = db.execute(
            "SELECT p.id, p.title, p.created_at, u.username AS author "
            "FROM posts p LEFT JOIN users u ON u.id = p.user_id "
            "WHERE p.board = 'inquiry' ORDER BY p.id DESC"
        ).fetchall()
    else:
        rows = db.execute(
            "SELECT p.id, p.title, p.created_at, u.username AS author "
            "FROM posts p LEFT JOIN users u ON u.id = p.user_id "
            "WHERE p.board = 'inquiry' AND p.user_id = ? ORDER BY p.id DESC",
            (current_user_id(),),
        ).fetchall()
    return render_template("inquiries.html", items=rows)


@app.get("/inquiries/new")
@login_required
def inquiries_new():
    return render_template("inquiry_form.html")


@app.post("/inquiries")
@login_required
def inquiries_create():
    ok, title, body, image_name, _ = _parse_post_form()
    if not ok:
        return redirect(url_for("inquiries_new"))
    db = get_db()
    db.execute(
        "INSERT INTO posts (board, user_id, title, body, image, created_at) "
        "VALUES ('inquiry', ?, ?, ?, ?, ?)",
        (current_user_id(), title, body, image_name,
         datetime.utcnow().isoformat(timespec="seconds")),
    )
    db.commit()
    flash("문의가 등록되었습니다.", "success")
    return redirect(url_for("inquiries_list"))


@app.get("/inquiries/<int:pid>")
@login_required
def inquiries_view(pid):
    post = _get_post(pid, "inquiry")
    if post["user_id"] != current_user_id() and not is_admin():
        abort(403)
    return render_template("inquiry_view.html", post=post)


@app.post("/inquiries/<int:pid>/delete")
@login_required
def inquiries_delete(pid):
    post = _get_post(pid, "inquiry")
    if post["user_id"] != current_user_id() and not is_admin():
        abort(403)
    delete_upload(post["image"])
    db = get_db()
    db.execute("DELETE FROM posts WHERE id=?", (pid,))
    db.commit()
    flash("문의를 삭제했습니다.", "success")
    return redirect(url_for("inquiries_list"))


# --- 자료실 (material): any user writes, all users read ---
def _can_edit_material(post) -> bool:
    return is_admin() or post["user_id"] == current_user_id()


@app.get("/materials")
@login_required
def materials_list():
    rows = get_db().execute(
        "SELECT p.id, p.title, p.created_at, u.username AS author, "
        "(SELECT COUNT(*) FROM post_attachments a WHERE a.post_id = p.id) AS attach_count "
        "FROM posts p LEFT JOIN users u ON u.id = p.user_id "
        "WHERE p.board = 'material' ORDER BY p.id DESC"
    ).fetchall()
    return render_template("materials.html", items=rows)


@app.get("/materials/new")
@login_required
def materials_new():
    return render_template("material_form.html", post=None, attachments=[])


@app.post("/materials")
@login_required
def materials_create():
    ok, title, body, image_name, _ = _parse_post_form()
    if not ok:
        return redirect(url_for("materials_new"))
    db = get_db()
    cur = db.execute(
        "INSERT INTO posts (board, user_id, title, body, image, created_at) "
        "VALUES ('material', ?, ?, ?, ?, ?)",
        (current_user_id(), title, body, image_name,
         datetime.utcnow().isoformat(timespec="seconds")),
    )
    save_attachments(db, cur.lastrowid, request.files.getlist("attachments"))
    db.commit()
    flash("자료가 등록되었습니다.", "success")
    return redirect(url_for("materials_list"))


@app.get("/materials/<int:pid>")
@login_required
def materials_view(pid):
    post = _get_post(pid, "material")
    attachments = get_post_attachments(get_db(), pid)
    return render_template(
        "material_view.html", post=post, attachments=attachments,
        can_edit=_can_edit_material(post),
    )


@app.get("/materials/<int:pid>/edit")
@login_required
def materials_edit_form(pid):
    post = _get_post(pid, "material")
    if not _can_edit_material(post):
        abort(403)
    attachments = get_post_attachments(get_db(), pid)
    return render_template("material_form.html", post=post, attachments=attachments)


@app.post("/materials/<int:pid>/edit")
@login_required
def materials_edit(pid):
    post = _get_post(pid, "material")
    if not _can_edit_material(post):
        abort(403)
    ok, title, body, new_image, remove_image = _parse_post_form()
    if not ok:
        return redirect(url_for("materials_edit_form", pid=pid))
    image = post["image"]
    if new_image:
        delete_upload(image)
        image = new_image
    elif remove_image:
        delete_upload(image)
        image = None
    db = get_db()
    db.execute(
        "UPDATE posts SET title=?, body=?, image=? WHERE id=?",
        (title, body, image, pid),
    )
    remove_aids = [int(x) for x in request.form.getlist("remove_attach") if x.isdigit()]
    if remove_aids:
        delete_post_attachments(db, pid, only_ids=remove_aids)
    save_attachments(db, pid, request.files.getlist("attachments"))
    db.commit()
    flash("자료가 수정되었습니다.", "success")
    return redirect(url_for("materials_view", pid=pid))


@app.post("/materials/<int:pid>/delete")
@login_required
def materials_delete(pid):
    post = _get_post(pid, "material")
    if not _can_edit_material(post):
        abort(403)
    delete_upload(post["image"])
    db = get_db()
    delete_post_attachments(db, pid)
    db.execute("DELETE FROM posts WHERE id=?", (pid,))
    db.commit()
    flash("자료를 삭제했습니다.", "success")
    return redirect(url_for("materials_list"))


@app.get("/attachments/<int:aid>")
@login_required
def attachment_download(aid):
    row = get_db().execute(
        "SELECT a.original_name, a.stored_name, a.mime, p.board, p.user_id "
        "FROM post_attachments a JOIN posts p ON p.id = a.post_id "
        "WHERE a.id = ?", (aid,)
    ).fetchone()
    if not row:
        abort(404)
    # Inquiry attachments are private; restrict to author or admin
    if row["board"] == "inquiry" and not is_admin() and row["user_id"] != current_user_id():
        abort(403)
    return send_from_directory(
        UPLOAD_DIR, row["stored_name"],
        as_attachment=True, download_name=row["original_name"],
    )


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
        cur = db.execute(
            "INSERT INTO users (username, password_hash, is_admin, created_at) "
            "VALUES (?, ?, ?, ?)",
            (
                username,
                generate_password_hash(password),
                1 if make_admin else 0,
                datetime.utcnow().isoformat(timespec="seconds"),
            ),
        )
        new_uid = cur.lastrowid
        folder_count = _copy_template_folders(db, new_uid)
        db.commit()
        msg = f"사용자 '{username}'을(를) 추가했습니다."
        if folder_count:
            msg += f" (템플릿 폴더 {folder_count}개 복사됨)"
        flash(msg, "success")
    except sqlite3.IntegrityError:
        db.rollback()
        flash(f"아이디 '{username}'은(는) 이미 존재합니다.", "error")
    except Exception:
        db.rollback()
        raise
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
    # Clean up post images and attachments uploaded by this user
    post_images = db.execute(
        "SELECT image FROM posts WHERE user_id = ? AND image IS NOT NULL",
        (uid,),
    ).fetchall()
    for r in post_images:
        delete_upload(r["image"])
    user_post_ids = [
        r["id"] for r in db.execute(
            "SELECT id FROM posts WHERE user_id = ?", (uid,)
        ).fetchall()
    ]
    for pid in user_post_ids:
        delete_post_attachments(db, pid)
    db.execute("DELETE FROM posts    WHERE user_id = ?", (uid,))
    db.execute("DELETE FROM exams    WHERE user_id = ?", (uid,))
    db.execute("DELETE FROM settings WHERE user_id = ?", (uid,))
    db.execute("DELETE FROM users    WHERE id = ?", (uid,))
    db.commit()
    flash(f"사용자 '{row['username']}' 및 관련 데이터를 삭제했습니다.", "success")
    return redirect(url_for("users_list"))


# ---------- 학습 게시판 (video board) ----------
_YT_PATTERNS = (
    re.compile(r"youtu\.be/([A-Za-z0-9_-]{6,})"),
    re.compile(r"youtube\.com/watch\?(?:[^#]*&)?v=([A-Za-z0-9_-]{6,})"),
    re.compile(r"youtube\.com/embed/([A-Za-z0-9_-]{6,})"),
    re.compile(r"youtube\.com/shorts/([A-Za-z0-9_-]{6,})"),
)


def _yt_video_id(url):
    if not url:
        return None
    for pat in _YT_PATTERNS:
        m = pat.search(url)
        if m:
            return m.group(1)
    return None


def _yt_embed(url):
    vid = _yt_video_id(url)
    return f"https://www.youtube.com/embed/{vid}" if vid else None


def _yt_thumb(url):
    vid = _yt_video_id(url)
    return f"https://img.youtube.com/vi/{vid}/hqdefault.jpg" if vid else None


def _learning_folder_or_404(fid):
    row = get_db().execute(
        "SELECT * FROM learning_folders WHERE id=?", (fid,)
    ).fetchone()
    if not row:
        abort(404)
    return row


def _learning_breadcrumb(fid):
    db = get_db()
    chain = []
    cur = fid
    seen = set()
    while cur is not None and cur not in seen:
        seen.add(cur)
        row = db.execute(
            "SELECT id, parent_id, name FROM learning_folders WHERE id=?",
            (cur,),
        ).fetchone()
        if not row:
            break
        chain.append(row)
        cur = row["parent_id"]
    return list(reversed(chain))


def _learning_descendants(fid):
    db = get_db()
    out = []
    stack = [fid]
    while stack:
        cur = stack.pop()
        out.append(cur)
        children = db.execute(
            "SELECT id FROM learning_folders WHERE parent_id=?", (cur,)
        ).fetchall()
        stack.extend(c["id"] for c in children)
    return out


@app.get("/learning")
@login_required
def learning_index():
    roots = get_db().execute(
        "SELECT id, name FROM learning_folders "
        "WHERE parent_id IS NULL ORDER BY position, id"
    ).fetchall()
    return render_template("learning_index.html", roots=roots)


@app.get("/learning/folder/<int:fid>")
@login_required
def learning_folder(fid):
    folder = _learning_folder_or_404(fid)
    db = get_db()
    subfolders = db.execute(
        "SELECT id, name FROM learning_folders "
        "WHERE parent_id=? ORDER BY position, id",
        (fid,),
    ).fetchall()
    video_rows = db.execute(
        "SELECT id, title, url FROM learning_videos "
        "WHERE folder_id=? ORDER BY position, id",
        (fid,),
    ).fetchall()
    videos = [
        {"id": v["id"], "title": v["title"], "url": v["url"],
         "thumb": _yt_thumb(v["url"])}
        for v in video_rows
    ]
    return render_template(
        "learning_folder.html",
        folder=folder,
        subfolders=subfolders,
        videos=videos,
        breadcrumb=_learning_breadcrumb(fid),
        is_root=(folder["parent_id"] is None),
    )


@app.post("/learning/folder/new")
@admin_required
def learning_folder_new():
    parent_id = request.form.get("parent_id", type=int)
    name = (request.form.get("name") or "").strip()
    if not parent_id:
        flash("최상위(초등/중등/고등) 아래에서만 폴더를 만들 수 있습니다.", "error")
        return redirect(url_for("learning_index"))
    if not name:
        flash("폴더 이름을 입력하세요.", "error")
        return redirect(url_for("learning_folder", fid=parent_id))
    db = get_db()
    if not db.execute(
        "SELECT 1 FROM learning_folders WHERE id=?", (parent_id,)
    ).fetchone():
        abort(404)
    pos = db.execute(
        "SELECT COALESCE(MAX(position), -1) + 1 FROM learning_folders "
        "WHERE parent_id=?",
        (parent_id,),
    ).fetchone()[0]
    db.execute(
        "INSERT INTO learning_folders "
        "(parent_id, name, position, created_at) VALUES (?, ?, ?, ?)",
        (parent_id, name, pos,
         datetime.utcnow().isoformat(timespec="seconds")),
    )
    db.commit()
    flash("폴더가 추가되었습니다.", "success")
    return redirect(url_for("learning_folder", fid=parent_id))


@app.post("/learning/folder/<int:fid>/rename")
@admin_required
def learning_folder_rename(fid):
    folder = _learning_folder_or_404(fid)
    if folder["parent_id"] is None:
        flash("최상위 폴더(초등/중등/고등)는 이름을 바꿀 수 없습니다.", "error")
        return redirect(url_for("learning_folder", fid=fid))
    name = (request.form.get("name") or "").strip()
    if not name:
        flash("폴더 이름을 입력하세요.", "error")
        return redirect(url_for("learning_folder", fid=fid))
    db = get_db()
    db.execute("UPDATE learning_folders SET name=? WHERE id=?", (name, fid))
    db.commit()
    flash("폴더 이름이 변경되었습니다.", "success")
    return redirect(url_for("learning_folder", fid=fid))


@app.post("/learning/folder/<int:fid>/delete")
@admin_required
def learning_folder_delete(fid):
    folder = _learning_folder_or_404(fid)
    if folder["parent_id"] is None:
        flash("최상위 폴더(초등/중등/고등)는 삭제할 수 없습니다.", "error")
        return redirect(url_for("learning_folder", fid=fid))
    parent_id = folder["parent_id"]
    db = get_db()
    ids = _learning_descendants(fid)
    placeholders = ",".join("?" * len(ids))
    db.execute(
        f"DELETE FROM learning_videos WHERE folder_id IN ({placeholders})",
        ids,
    )
    db.execute(
        f"DELETE FROM learning_folders WHERE id IN ({placeholders})",
        ids,
    )
    db.commit()
    flash("폴더와 하위 항목을 삭제했습니다.", "success")
    return redirect(url_for("learning_folder", fid=parent_id))


@app.post("/learning/video/new")
@admin_required
def learning_video_new():
    folder_id = request.form.get("folder_id", type=int)
    title = (request.form.get("title") or "").strip()
    url = (request.form.get("url") or "").strip()
    description = (request.form.get("description") or "").strip()
    if not folder_id:
        abort(400)
    if not title or not url:
        flash("제목과 영상 링크는 필수입니다.", "error")
        return redirect(url_for("learning_folder", fid=folder_id))
    db = get_db()
    if not db.execute(
        "SELECT 1 FROM learning_folders WHERE id=?", (folder_id,)
    ).fetchone():
        abort(404)
    pos = db.execute(
        "SELECT COALESCE(MAX(position), -1) + 1 FROM learning_videos "
        "WHERE folder_id=?",
        (folder_id,),
    ).fetchone()[0]
    db.execute(
        "INSERT INTO learning_videos "
        "(folder_id, title, url, description, position, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (folder_id, title, url, description, pos,
         datetime.utcnow().isoformat(timespec="seconds")),
    )
    db.commit()
    flash("영상이 추가되었습니다.", "success")
    return redirect(url_for("learning_folder", fid=folder_id))


@app.get("/learning/video/<int:vid>")
@login_required
def learning_video_view(vid):
    v = get_db().execute(
        "SELECT * FROM learning_videos WHERE id=?", (vid,)
    ).fetchone()
    if not v:
        abort(404)
    return render_template(
        "learning_video.html",
        video=v,
        embed=_yt_embed(v["url"]),
        breadcrumb=_learning_breadcrumb(v["folder_id"]),
    )


@app.post("/learning/video/<int:vid>/edit")
@admin_required
def learning_video_edit(vid):
    v = get_db().execute(
        "SELECT * FROM learning_videos WHERE id=?", (vid,)
    ).fetchone()
    if not v:
        abort(404)
    title = (request.form.get("title") or "").strip()
    url = (request.form.get("url") or "").strip()
    description = (request.form.get("description") or "").strip()
    if not title or not url:
        flash("제목과 영상 링크는 필수입니다.", "error")
        return redirect(url_for("learning_video_view", vid=vid))
    db = get_db()
    db.execute(
        "UPDATE learning_videos SET title=?, url=?, description=? WHERE id=?",
        (title, url, description, vid),
    )
    db.commit()
    flash("영상이 수정되었습니다.", "success")
    return redirect(url_for("learning_video_view", vid=vid))


@app.post("/learning/video/<int:vid>/delete")
@admin_required
def learning_video_delete(vid):
    v = get_db().execute(
        "SELECT folder_id FROM learning_videos WHERE id=?", (vid,)
    ).fetchone()
    if not v:
        abort(404)
    folder_id = v["folder_id"]
    db = get_db()
    db.execute("DELETE FROM learning_videos WHERE id=?", (vid,))
    db.commit()
    flash("영상을 삭제했습니다.", "success")
    return redirect(url_for("learning_folder", fid=folder_id))


# Run schema migration at import time so it works under both
# `python app.py` (dev) and `gunicorn app:app` (production).
init_db()


if __name__ == "__main__":
    debug = os.environ.get("FLASK_ENV", "development") != "production"
    port = int(os.environ.get("PORT", 5001))
    host = "127.0.0.1" if debug else "0.0.0.0"
    app.run(debug=debug, host=host, port=port)
