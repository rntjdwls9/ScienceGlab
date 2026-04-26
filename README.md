# MathGlab

수학 문제 관리 웹앱. 문제 등록·검색·시험지 생성·인쇄까지.

Flask + SQLite + Tailwind CDN. 학원/단체용 다중 사용자 지원 (관리자/사용자 권한 분리).

## 주요 기능

- **문제 관리** (관리자): 이미지 + 메타데이터(단원/난이도/출처/태그/정답)로 문제 등록·수정·삭제
- **단원 폴더 계층** (관리자): 단원 안에 하위 단원 무한 중첩
- **사용자 관리** (관리자): 개별 ID/비밀번호 부여, 관리자 권한 부여
- **문제 검색**: 단원·난이도·출처·태그 필터, 카드 다중 선택
- **시험지 생성**:
  - 다단(1·2·3단), 페이지당 문제 수, 문제 간격 조정
  - 제목 글씨체·크기·정렬 커스텀
  - 헤더 항목 표시/숨김 (학원 로고·날짜·이름·점수 등)
  - A4 인쇄 최적화 (브라우저 자동 헤더/푸터 제거)
- **시험지 보관함** (사용자별): 생성한 시험지 저장 후 재사용
- **학원 정보** (사용자별 설정): 로고 + 이름 → 모든 시험지 헤더에 자동 삽입

## 로컬 개발

```bash
pip install -r requirements.txt
python app.py
```

브라우저에서 http://127.0.0.1:5000 접속.

기본 관리자 계정: `admin` / `admin` (첫 실행 시 자동 생성, 로그인 후 비밀번호 변경 권장).

## 환경 변수

| 변수 | 기본값 | 설명 |
|---|---|---|
| `SECRET_KEY` | `mathglab-dev-secret-CHANGE-ME` | Flask 세션 서명 키 (프로덕션 필수) |
| `UPLOAD_DIR` | `static/uploads` | 업로드 이미지 저장 경로 |
| `DB_PATH` | `problems.db` | SQLite DB 파일 경로 |
| `FLASK_ENV` | `development` | `production` 시 debug 모드 끔 |
| `PORT` | `5000` | 서버 포트 (Render에서 자동 주입) |

## 배포 (Render)

`Procfile`과 `requirements.txt`로 Render Web Service 자동 인식.

기본 설정:
- **Build Command**: `pip install -r requirements.txt`
- **Start Command**: `gunicorn app:app`
- **환경변수**: `SECRET_KEY` (랜덤 문자열), `FLASK_ENV=production`

> ⚠️ **무료 티어 주의**: 업로드 이미지와 SQLite DB는 컨테이너 안에 저장되므로 **재배포·재시작 시 모두 사라집니다**. 또한 무료 인스턴스는 15분 무활동 시 슬립.

영구 저장이 필요하면 다음 중 하나:

1. **Render Persistent Disk** (월 $1+, 1GB)
   - Disk Mount Path: `/var/data`
   - 환경변수 추가: `UPLOAD_DIR=/var/data/uploads`, `DB_PATH=/var/data/problems.db`

2. **외부 스토리지 + DB**
   - 이미지: S3 / Cloudflare R2 등
   - DB: Render PostgreSQL (무료 제공) 등으로 마이그레이션 — 코드 변경 필요

## 기술 스택

- Backend: Flask 3.x, SQLite, Werkzeug (인증)
- Frontend: Jinja2 templates, Tailwind CSS (CDN), vanilla JS
- WSGI: gunicorn (프로덕션)
