-- 2026-05-08「患者导出 parsed jpg / 预览路径」：`dataset_image_asset.parsed_path`
-- 在有数据的 MySQL / TiDB 上执行一次；pytest 使用的 SQLite 会删库重建，无需手动跑。

ALTER TABLE dataset_image_asset
  ADD COLUMN parsed_path VARCHAR(1024) NULL
  AFTER original_path;
