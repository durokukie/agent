-- Alembic 이전(create_all) 에이전트가 만든 실제 DB 의 표 모양 — 2026-09-22 노트북 ~/.kukie/kukie.db 의 sqlite_master 를 그대로 (데이터 없음).
-- 0001_baseline 이 이 모양과 같은지 tests/test_migrations.py 가 대조한다. 손으로 고치지 말 것.
CREATE TABLE tbl_chat_session (
	id VARCHAR(36) NOT NULL, 
	user_id VARCHAR(64) NOT NULL, 
	title VARCHAR(200) NOT NULL, 
	current_mode VARCHAR(20) NOT NULL, 
	installation_id VARCHAR(64), 
	context_name VARCHAR(200) NOT NULL, 
	namespace VARCHAR(63) NOT NULL, 
	cluster_fingerprint VARCHAR(200), 
	version INTEGER NOT NULL, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	team_id VARCHAR(64), 
	cluster_id VARCHAR(64), 
	shared BOOLEAN NOT NULL, 
	PRIMARY KEY (id)
);
CREATE INDEX ix_tbl_chat_session_cluster_id ON tbl_chat_session (cluster_id);
CREATE INDEX ix_tbl_chat_session_user_id ON tbl_chat_session (user_id);
CREATE INDEX ix_tbl_chat_session_team_id ON tbl_chat_session (team_id);
CREATE TABLE tbl_chat_run (
	id VARCHAR(36) NOT NULL, 
	session_id VARCHAR(36) NOT NULL, 
	request_id VARCHAR(64) NOT NULL, 
	turn_no INTEGER NOT NULL, 
	kind VARCHAR(20) NOT NULL, 
	mode VARCHAR(20) NOT NULL, 
	status VARCHAR(20) NOT NULL, 
	input_text TEXT, 
	response_payload JSON, 
	agent_messages JSON, 
	history_format_version INTEGER NOT NULL, 
	usage_summary JSON, 
	started_at DATETIME NOT NULL, 
	finished_at DATETIME, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_run_request UNIQUE (session_id, request_id), 
	CONSTRAINT uq_run_turn UNIQUE (session_id, turn_no), 
	FOREIGN KEY(session_id) REFERENCES tbl_chat_session (id) ON DELETE RESTRICT
);
CREATE INDEX ix_tbl_chat_run_session_id ON tbl_chat_run (session_id);
CREATE TABLE tbl_action_plan (
	id VARCHAR(64) NOT NULL, 
	run_id VARCHAR(36) NOT NULL, 
	tool_call_id VARCHAR(64) NOT NULL, 
	tool_name VARCHAR(50) NOT NULL, 
	status VARCHAR(30) NOT NULL, 
	risk VARCHAR(20) NOT NULL, 
	plan_payload JSON NOT NULL, 
	request_hash VARCHAR(64) NOT NULL, 
	decision JSON, 
	execution_result JSON, 
	failure_reason VARCHAR(40), 
	applied_at DATETIME, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_plan_tool_call UNIQUE (run_id, tool_call_id), 
	CONSTRAINT ck_plan_decision_present CHECK (status NOT IN ('APPROVED', 'EXECUTING', 'APPLIED', 'EFFECT_VERIFIED') OR decision IS NOT NULL), 
	CONSTRAINT ck_plan_applied_has_result CHECK (status NOT IN ('APPLIED', 'EFFECT_VERIFIED') OR (execution_result IS NOT NULL AND applied_at IS NOT NULL)), 
	CONSTRAINT ck_plan_failed_has_reason CHECK (status <> 'FAILED' OR failure_reason IS NOT NULL), 
	FOREIGN KEY(run_id) REFERENCES tbl_chat_run (id) ON DELETE RESTRICT
);
CREATE INDEX ix_tbl_action_plan_run_id ON tbl_action_plan (run_id);
CREATE INDEX ix_tbl_action_plan_tool_call_id ON tbl_action_plan (tool_call_id);
CREATE TABLE tbl_cluster (
	id VARCHAR(36) NOT NULL, 
	team_id VARCHAR(64), 
	registered_by VARCHAR(64) NOT NULL, 
	name VARCHAR(100) NOT NULL, 
	provider VARCHAR(20) NOT NULL, 
	api_server VARCHAR(300) NOT NULL, 
	ca_data TEXT, 
	insecure BOOLEAN NOT NULL, 
	credential_encrypted TEXT NOT NULL, 
	context_name VARCHAR(200) NOT NULL, 
	default_namespace VARCHAR(63) NOT NULL, 
	fingerprint VARCHAR(64) NOT NULL, 
	status VARCHAR(20) NOT NULL, 
	last_checked_at DATETIME, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT ck_cluster_https CHECK (api_server LIKE 'https://%')
);
CREATE INDEX ix_tbl_cluster_registered_by ON tbl_cluster (registered_by);
CREATE INDEX ix_tbl_cluster_fingerprint ON tbl_cluster (fingerprint);
CREATE INDEX ix_tbl_cluster_team_id ON tbl_cluster (team_id);
CREATE UNIQUE INDEX uq_cluster_personal_name ON tbl_cluster (registered_by, name) WHERE team_id IS NULL;
CREATE UNIQUE INDEX uq_cluster_team_name ON tbl_cluster (team_id, name) WHERE team_id IS NOT NULL;
