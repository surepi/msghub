CREATE TABLE IF NOT EXISTS schema_state (
    id TINYINT PRIMARY KEY,
    seeded TINYINT NOT NULL DEFAULT 0
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS sites (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    name LONGTEXT NOT NULL,
    domain LONGTEXT NOT NULL DEFAULT (''),
    site_key VARCHAR(64) NOT NULL UNIQUE,
    allowed_origins LONGTEXT NOT NULL DEFAULT (''),
    webhook_url LONGTEXT NOT NULL DEFAULT (''),
    enabled BIGINT NOT NULL DEFAULT 1,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    welcome LONGTEXT NOT NULL DEFAULT (''),
    offline_text LONGTEXT NOT NULL DEFAULT (''),
    work_hours LONGTEXT NOT NULL DEFAULT (''),
    require_contact BIGINT NOT NULL DEFAULT 1,
    ai_enabled BIGINT NOT NULL DEFAULT 1,
    crawl_urls LONGTEXT NOT NULL DEFAULT ('')
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS messages (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    site_id BIGINT NOT NULL,
    name LONGTEXT NOT NULL DEFAULT (''),
    contact LONGTEXT NOT NULL DEFAULT (''),
    content LONGTEXT NOT NULL,
    page_url LONGTEXT NOT NULL DEFAULT (''),
    ip LONGTEXT NOT NULL DEFAULT (''),
    user_agent LONGTEXT NOT NULL DEFAULT (''),
    status VARCHAR(24) NOT NULL DEFAULT 'new',
    note LONGTEXT NOT NULL DEFAULT (''),
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    conversation_id BIGINT,
    INDEX idx_messages_site (site_id),
    INDEX idx_messages_status (status),
    INDEX idx_messages_created (created_at),
    INDEX idx_messages_conversation (conversation_id, site_id, id),
    FOREIGN KEY (site_id) REFERENCES sites(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS visitors (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    token VARCHAR(64) NOT NULL UNIQUE,
    name LONGTEXT NOT NULL DEFAULT (''),
    contact LONGTEXT NOT NULL DEFAULT (''),
    ip LONGTEXT NOT NULL DEFAULT (''),
    user_agent LONGTEXT NOT NULL DEFAULT (''),
    first_page LONGTEXT NOT NULL DEFAULT (''),
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    site_id BIGINT,
    FOREIGN KEY (site_id) REFERENCES sites(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS pending_uploads (
    path VARCHAR(96) PRIMARY KEY,
    owner VARCHAR(96) NOT NULL,
    expires_at DOUBLE NOT NULL,
    INDEX idx_pending_uploads_expiry (expires_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS conversations (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    site_id BIGINT NOT NULL,
    visitor_id BIGINT NOT NULL,
    status VARCHAR(24) NOT NULL DEFAULT 'waiting',
    page_url LONGTEXT NOT NULL DEFAULT (''),
    last_msg_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_preview LONGTEXT NOT NULL DEFAULT (''),
    unread BIGINT NOT NULL DEFAULT 0,
    read_upto BIGINT NOT NULL DEFAULT 0,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    closed_at DATETIME,
    UNIQUE (site_id, visitor_id),
    ai_waiting_for_agent BIGINT NOT NULL DEFAULT 0,
    ai_priority_dismissed_upto BIGINT NOT NULL DEFAULT 0,
    auto_handled_upto BIGINT NOT NULL DEFAULT 0,
    INDEX idx_conv_last (last_msg_at),
    FOREIGN KEY (site_id) REFERENCES sites(id) ON DELETE CASCADE,
    FOREIGN KEY (visitor_id) REFERENCES visitors(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS chat_messages (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    conversation_id BIGINT NOT NULL,
    sender VARCHAR(24) NOT NULL,
    content LONGTEXT NOT NULL,
    qq_message_id LONGTEXT NOT NULL DEFAULT (''),
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    qq_state VARCHAR(24) NOT NULL DEFAULT '',
    msg_type VARCHAR(24) NOT NULL DEFAULT 'text',
    INDEX idx_chat_conv (conversation_id, id),
    INDEX idx_chat_conv_created (conversation_id, created_at, id),
    FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS qq_inbound (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    event_key CHAR(64) NOT NULL UNIQUE,
    app_id LONGTEXT NOT NULL,
    payload LONGTEXT,
    received_at DOUBLE NOT NULL,
    binding_version BIGINT NOT NULL,
    default_conv_id BIGINT,
    default_set_at DOUBLE NOT NULL,
    target_conv_id BIGINT,
    `implicit` BIGINT NOT NULL DEFAULT 0,
    status VARCHAR(24) NOT NULL DEFAULT 'pending',
    attempts BIGINT NOT NULL DEFAULT 0,
    next_attempt_at DOUBLE NOT NULL DEFAULT 0,
    INDEX idx_qq_inbound_due (status, next_attempt_at, id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS qq_agent (
    id BIGINT PRIMARY KEY CHECK (id = 1),
    openid LONGTEXT NOT NULL DEFAULT (''),
    bound_at DATETIME,
    online BIGINT NOT NULL DEFAULT 1,
    current_conv_id BIGINT,
    current_set_at DOUBLE NOT NULL DEFAULT 0,
    last_msg_id LONGTEXT NOT NULL DEFAULT (''),
    last_msg_at DOUBLE NOT NULL DEFAULT 0,
    passive_used BIGINT NOT NULL DEFAULT 0,
    bind_code LONGTEXT NOT NULL DEFAULT (''),
    bind_expires DOUBLE NOT NULL DEFAULT 0,
    last_event_at DATETIME,
    binding_version BIGINT NOT NULL DEFAULT 0
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS qq_forwards (
    qq_msg_id VARCHAR(255) PRIMARY KEY,
    conversation_id BIGINT NOT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS conversation_summaries (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    conversation_id BIGINT NOT NULL,
    source_upto BIGINT NOT NULL,
    status VARCHAR(24) NOT NULL,
    error_code LONGTEXT NOT NULL DEFAULT (''),
    model LONGTEXT NOT NULL DEFAULT (''),
    summary LONGTEXT NOT NULL DEFAULT (''),
    needs LONGTEXT NOT NULL DEFAULT ('[]'),
    contacts LONGTEXT NOT NULL DEFAULT ('[]'),
    sources LONGTEXT NOT NULL DEFAULT ('[]'),
    source_count BIGINT NOT NULL DEFAULT 0,
    truncated BIGINT NOT NULL DEFAULT 0,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (conversation_id, source_upto),
    INDEX idx_conversation_summaries_conv (conversation_id, source_upto),
    FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS conversation_triage (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    conversation_id BIGINT NOT NULL,
    source_upto BIGINT NOT NULL,
    status VARCHAR(24) NOT NULL,
    error_code LONGTEXT NOT NULL DEFAULT (''),
    model LONGTEXT NOT NULL DEFAULT (''),
    category LONGTEXT NOT NULL DEFAULT ('other'),
    category_evidence LONGTEXT NOT NULL DEFAULT ('{}'),
    priority LONGTEXT NOT NULL DEFAULT ('normal'),
    priority_evidence LONGTEXT NOT NULL DEFAULT ('{}'),
    tag_suggestions LONGTEXT NOT NULL DEFAULT ('[]'),
    sources LONGTEXT NOT NULL DEFAULT ('[]'),
    source_count BIGINT NOT NULL DEFAULT 0,
    truncated BIGINT NOT NULL DEFAULT 0,
    dismissed BIGINT NOT NULL DEFAULT 0,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (conversation_id, source_upto),
    INDEX idx_conversation_triage_conv (conversation_id, source_upto),
    FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS qq_deliveries (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    conversation_id BIGINT NOT NULL,
    recipient LONGTEXT NOT NULL,
    kind VARCHAR(24) NOT NULL DEFAULT 'visitor',
    summary_id BIGINT,
    triage_id BIGINT,
    event_key VARCHAR(128),
    reason LONGTEXT NOT NULL DEFAULT (''),
    summary LONGTEXT NOT NULL DEFAULT (''),
    inbox_message_id BIGINT,
    status VARCHAR(24) NOT NULL DEFAULT 'pending',
    attempts BIGINT NOT NULL DEFAULT 0,
    last_error LONGTEXT NOT NULL DEFAULT (''),
    warning LONGTEXT NOT NULL DEFAULT (''),
    last_attempt_at DOUBLE NOT NULL DEFAULT 0,
    next_attempt_at DOUBLE NOT NULL DEFAULT 0,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    sent_at DATETIME,
    INDEX idx_qq_deliveries_status (status, next_attempt_at, id),
    INDEX idx_qq_deliveries_conv (conversation_id, id),
    UNIQUE INDEX idx_qq_delivery_event (event_key),
    FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE,
    FOREIGN KEY (summary_id) REFERENCES conversation_summaries(id) ON DELETE SET NULL,
    FOREIGN KEY (triage_id) REFERENCES conversation_triage(id) ON DELETE SET NULL,
    FOREIGN KEY (inbox_message_id) REFERENCES messages(id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS qq_delivery_messages (
    delivery_id BIGINT NOT NULL,
    message_id BIGINT NOT NULL UNIQUE,
    PRIMARY KEY (delivery_id, message_id),
    FOREIGN KEY (delivery_id) REFERENCES qq_deliveries(id) ON DELETE CASCADE,
    FOREIGN KEY (message_id) REFERENCES chat_messages(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS qq_delivery_parts (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    delivery_id BIGINT NOT NULL,
    `sequence` BIGINT NOT NULL,
    kind VARCHAR(24) NOT NULL,
    content LONGTEXT NOT NULL,
    status VARCHAR(24) NOT NULL DEFAULT 'pending',
    attempts BIGINT NOT NULL DEFAULT 0,
    last_error LONGTEXT NOT NULL DEFAULT (''),
    qq_message_id LONGTEXT NOT NULL DEFAULT (''),
    manually_confirmed BIGINT NOT NULL DEFAULT 0,
    sent_at DATETIME,
    UNIQUE (delivery_id, sequence),
    FOREIGN KEY (delivery_id) REFERENCES qq_deliveries(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS qq_notification_sources (
    delivery_id BIGINT NOT NULL,
    message_id BIGINT NOT NULL,
    PRIMARY KEY (delivery_id, message_id),
    INDEX idx_qq_notification_message (message_id, delivery_id),
    FOREIGN KEY (delivery_id) REFERENCES qq_deliveries(id) ON DELETE CASCADE,
    FOREIGN KEY (message_id) REFERENCES chat_messages(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS ai_decisions (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    conversation_id BIGINT NOT NULL,
    source_upto BIGINT NOT NULL,
    message_id BIGINT,
    inbox_message_id BIGINT,
    qq_delivery_id BIGINT,
    outcome LONGTEXT NOT NULL,
    `trigger` LONGTEXT NOT NULL,
    reason_code LONGTEXT NOT NULL,
    reason LONGTEXT NOT NULL,
    reason_origin LONGTEXT NOT NULL,
    model LONGTEXT NOT NULL DEFAULT (''),
    question LONGTEXT NOT NULL,
    question_truncated BIGINT NOT NULL DEFAULT 0,
    answer LONGTEXT NOT NULL DEFAULT (''),
    context LONGTEXT NOT NULL DEFAULT (''),
    sources LONGTEXT NOT NULL DEFAULT ('[]'),
    source_count BIGINT NOT NULL DEFAULT 0,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (conversation_id, source_upto),
    INDEX idx_ai_decisions_message (message_id),
    INDEX idx_ai_decisions_conv (conversation_id, id),
    INDEX idx_ai_decisions_delivery (qq_delivery_id),
    FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE,
    FOREIGN KEY (message_id) REFERENCES chat_messages(id) ON DELETE SET NULL,
    FOREIGN KEY (inbox_message_id) REFERENCES messages(id) ON DELETE SET NULL,
    FOREIGN KEY (qq_delivery_id) REFERENCES qq_deliveries(id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS conversation_triage_jobs (
    conversation_id BIGINT PRIMARY KEY,
    source_upto BIGINT NOT NULL,
    due_at DOUBLE NOT NULL,
    last_run_at DOUBLE NOT NULL DEFAULT 0,
    attempts BIGINT NOT NULL DEFAULT 0,
    FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS settings (
    `key` VARCHAR(128) PRIMARY KEY,
    value LONGTEXT NOT NULL DEFAULT ('')
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS qa (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    site_id BIGINT,
    question LONGTEXT NOT NULL,
    answer LONGTEXT NOT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (site_id) REFERENCES sites(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS kb_pages (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    site_id BIGINT NOT NULL,
    url LONGTEXT NOT NULL,
    title LONGTEXT NOT NULL DEFAULT (''),
    text LONGTEXT NOT NULL DEFAULT (''),
    fetched_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    url_hash BINARY(32) GENERATED ALWAYS AS (UNHEX(SHA2(url, 256))) STORED,
    UNIQUE (site_id, url_hash),
    FOREIGN KEY (site_id) REFERENCES sites(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS quick_replies (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    site_id BIGINT,
    title LONGTEXT NOT NULL,
    content LONGTEXT NOT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_quick_replies_site (site_id),
    FOREIGN KEY (site_id) REFERENCES sites(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS conversation_notes (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    conversation_id BIGINT NOT NULL,
    content LONGTEXT NOT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_conversation_notes (conversation_id, id),
    FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS tags (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    name VARCHAR(30) NOT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    name_folded VARCHAR(30) GENERATED ALWAYS AS (LOWER(name)) STORED,
    UNIQUE (name_folded)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS conversation_ai_tag_blocks (
    conversation_id BIGINT NOT NULL,
    tag_id BIGINT NOT NULL,
    PRIMARY KEY (conversation_id, tag_id),
    FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE,
    FOREIGN KEY (tag_id) REFERENCES tags(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE IF NOT EXISTS conversation_tags (
    conversation_id BIGINT NOT NULL,
    tag_id BIGINT NOT NULL,
    PRIMARY KEY (conversation_id, tag_id),
    INDEX idx_conversation_tags_tag (tag_id, conversation_id),
    FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE,
    FOREIGN KEY (tag_id) REFERENCES tags(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;
