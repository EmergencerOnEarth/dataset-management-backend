-- P0: part Content-Range end can exceed signed INT32 (2_147_483_647) for large zip uploads.
-- Apply on MySQL before re-testing multipart uploads > ~2GB.

ALTER TABLE dataset_upload_part
  MODIFY COLUMN range_start BIGINT NOT NULL DEFAULT 0,
  MODIFY COLUMN range_end BIGINT NOT NULL DEFAULT 0;
