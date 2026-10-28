-- AutoSub Studio: MySQL schema (also works on MariaDB and TiDB Cloud).
-- The app runs this file automatically on start-up, so you normally never
-- have to run it by hand. Every statement is safe to run more than once.
--
-- Create the database first (once):
--   CREATE DATABASE autosub CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
--
-- Tables:  users 1---* jobs 1---* cues
--                         jobs 1---* renders

-- A person. Visitors who have not signed up get a "guest" row (no email or
-- password) so that every project always belongs to somebody.
CREATE TABLE IF NOT EXISTS users (
    id            CHAR(32)      NOT NULL,
    email         VARCHAR(255)  NULL,
    display_name  VARCHAR(80)   NOT NULL,
    password_hash VARCHAR(255)  NULL,
    is_guest      TINYINT(1)    NOT NULL DEFAULT 1,
    created_at    DATETIME      NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_users_email (email)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- One uploaded video and the state of its transcription.
CREATE TABLE IF NOT EXISTS jobs (
    id              CHAR(32)      NOT NULL,
    owner           CHAR(32)      NOT NULL,
    original_name   VARCHAR(255)  NOT NULL,
    video_path      VARCHAR(600)  NOT NULL,
    duration_sec    DOUBLE        NULL,
    width           INT           NULL,
    height          INT           NULL,
    language        VARCHAR(32)   NULL,
    status          VARCHAR(20)   NOT NULL,
    progress        INT           NOT NULL DEFAULT 0,
    message         VARCHAR(500)  NULL,
    transcript_json MEDIUMTEXT    NULL,
    created_at      DATETIME      NOT NULL,
    updated_at      DATETIME      NOT NULL,
    PRIMARY KEY (id),
    KEY idx_jobs_owner_created (owner, created_at),
    CONSTRAINT fk_jobs_owner FOREIGN KEY (owner) REFERENCES users (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- The subtitle lines of a job. Times are integer milliseconds.
CREATE TABLE IF NOT EXISTS cues (
    id          INT           NOT NULL AUTO_INCREMENT,
    job_id      CHAR(32)      NOT NULL,
    idx         INT           NOT NULL,
    start_ms    INT           NOT NULL,
    end_ms      INT           NOT NULL,
    text        TEXT          NOT NULL,
    words_json  MEDIUMTEXT    NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_cues_job_idx (job_id, idx),
    CONSTRAINT fk_cues_job FOREIGN KEY (job_id) REFERENCES jobs (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- One request to burn subtitles (in a chosen template) into the video.
CREATE TABLE IF NOT EXISTS renders (
    id           CHAR(32)      NOT NULL,
    job_id       CHAR(32)      NOT NULL,
    template     VARCHAR(40)   NOT NULL,
    status       VARCHAR(20)   NOT NULL,
    progress     INT           NOT NULL DEFAULT 0,
    output_path  VARCHAR(600)  NULL,
    message      VARCHAR(500)  NULL,
    created_at   DATETIME      NOT NULL,
    updated_at   DATETIME      NOT NULL,
    PRIMARY KEY (id),
    KEY idx_renders_job (job_id, created_at),
    CONSTRAINT fk_renders_job FOREIGN KEY (job_id) REFERENCES jobs (id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
