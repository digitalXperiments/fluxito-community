from app.models.conversation import AIProviderKey, ChatMessage, Conversation


def test_tables_named():
    assert Conversation.__tablename__ == "conversations"
    assert ChatMessage.__tablename__ == "chat_messages"
    assert AIProviderKey.__tablename__ == "ai_provider_keys"


def test_chat_message_has_jsonb_content_and_seq():
    cols = ChatMessage.__table__.columns
    assert {"content", "seq", "role", "conversation_id"} <= set(cols.keys())


def test_conversation_has_no_section_or_provider_columns():
    cols = set(Conversation.__table__.columns.keys())
    assert "origin_section" not in cols
    assert "provider" not in cols


def test_model_config_is_personal_and_single_provider():
    cols = AIProviderKey.__table__.columns
    assert set(cols.keys()) == {
        "id",
        "user_id",
        "base_url",
        "model",
        "api_key_encrypted",
        "created_at",
        "updated_at",
    }
    assert cols["user_id"].unique and not cols["user_id"].nullable
    assert not cols["base_url"].nullable and not cols["model"].nullable
