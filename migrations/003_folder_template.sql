-- 003_folder_template.sql
-- exam_folders 테이블에 템플릿 표식과 정렬 순서 컬럼 추가.
-- 적용 후 admin 사용자의 최상위 폴더 중 '중등1'~'중등3','고등1'~'고등3'을
-- 템플릿(is_template=1)으로 표시하고 sort_order를 1~6으로 부여.

ALTER TABLE exam_folders ADD COLUMN is_template INTEGER NOT NULL DEFAULT 0;
ALTER TABLE exam_folders ADD COLUMN sort_order  INTEGER NOT NULL DEFAULT 0;

UPDATE exam_folders
SET is_template = 1,
    sort_order  = CASE name
        WHEN '중등1' THEN 1
        WHEN '중등2' THEN 2
        WHEN '중등3' THEN 3
        WHEN '고등1' THEN 4
        WHEN '고등2' THEN 5
        WHEN '고등3' THEN 6
    END
WHERE parent_id IS NULL
  AND name IN ('중등1','중등2','중등3','고등1','고등2','고등3')
  AND user_id = (SELECT id FROM users WHERE is_admin = 1 LIMIT 1);
