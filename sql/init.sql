-- 个人skill
CREATE TABLE personal_skill (
    user_id      TEXT        NOT NULL,
    skill_name   TEXT        NOT NULL,
    description  TEXT        NOT NULL,
    markdown     TEXT        NOT NULL,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, skill_name)
);

CREATE TABLE personal_skill_script (
    user_id      TEXT        NOT NULL,
    skill_name   TEXT        NOT NULL,
    script_path  TEXT        NOT NULL,
    content      TEXT        NOT NULL,
    PRIMARY KEY (user_id, skill_name, script_path),
    FOREIGN KEY (user_id, skill_name)
        REFERENCES personal_skill(user_id, skill_name) ON DELETE CASCADE
);


-- 聊天记录
CREATE TABLE public.chat_session_state (
	session_id varchar(64) NOT NULL,
	user_id varchar(64) NOT NULL,
	state_json jsonb NOT NULL,
	created_at timestamp DEFAULT now() NOT NULL,
	updated_at timestamp DEFAULT now() NOT NULL,
	CONSTRAINT chat_session_state_pkey PRIMARY KEY (session_id)
);
CREATE INDEX ix_chat_session_state_user_id ON public.chat_session_state USING btree (user_id);
CREATE INDEX ix_chat_session_user_updated ON public.chat_session_state USING btree (user_id, updated_at);


-- sandbox workspace 快照
-- 命名卷在沙箱主机本地存活的"实时副本",快照是 PG 里的"备份",
-- 用于跨容器重建 / 命名卷意外丢失时的恢复。
CREATE TABLE user_workspace_snapshot (
    id           BIGSERIAL   PRIMARY KEY,
    user_id      TEXT        NOT NULL,
    label        TEXT,                                       -- 'auto-pre-close' / 'manual' / NULL
    tar_bytes    BYTEA       NOT NULL,                       -- gzip tar,排除 skills/personal_skills 子目录
    size_bytes   BIGINT      NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ix_user_workspace_snapshot_user_created
    ON user_workspace_snapshot (user_id, created_at DESC);
CREATE INDEX ix_user_workspace_snapshot_user_label_created
    ON user_workspace_snapshot (user_id, label, created_at DESC);
