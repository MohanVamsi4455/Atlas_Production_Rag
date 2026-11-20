import uuid
from psycopg.types.json import Jsonb
from app.db import connection
from app.config import settings

def history(tenant: str, conversation_id: uuid.UUID):
    with connection() as conn:
        rows = conn.execute("""SELECT role,content FROM (
            SELECT id,role,content,created_at FROM chat_messages
            WHERE tenant_id=%s AND conversation_id=%s ORDER BY id DESC LIMIT %s
        ) recent ORDER BY id ASC""", (tenant,conversation_id,settings().memory_turns*2)).fetchall()
    return rows

def save_turn(tenant: str, conversation_id: uuid.UUID, question: str, response: str, sources: list | None = None):
    """Save one question/answer pair. The answer's sources are kept so citations still work after a reload."""
    with connection() as conn:
        conn.execute("""INSERT INTO chat_messages(tenant_id,conversation_id,role,content)
                        VALUES (%s,%s,'user',%s)""", (tenant,conversation_id,question))
        conn.execute("""INSERT INTO chat_messages(tenant_id,conversation_id,role,content,sources)
                        VALUES (%s,%s,'assistant',%s,%s)""",
                     (tenant,conversation_id,response,Jsonb(sources) if sources else None))

def list_messages(tenant: str, conversation_id: uuid.UUID):
    with connection() as conn:
        return conn.execute("""SELECT role,content,sources,created_at FROM chat_messages
          WHERE tenant_id=%s AND conversation_id=%s ORDER BY id ASC LIMIT 100""",
          (tenant,conversation_id)).fetchall()

def list_conversations(tenant: str, limit: int = 50):
    """Newest conversations first. The title is the first question asked in each one."""
    with connection() as conn:
        return conn.execute("""SELECT conversation_id AS id,
                (array_agg(content ORDER BY id) FILTER (WHERE role='user'))[1] AS title,
                max(created_at) AS updated_at, count(*) AS messages
            FROM chat_messages WHERE tenant_id=%s
            GROUP BY conversation_id ORDER BY max(created_at) DESC LIMIT %s""", (tenant,limit)).fetchall()

def delete_conversation(tenant: str, conversation_id: uuid.UUID) -> int:
    with connection() as conn:
        return len(conn.execute("DELETE FROM chat_messages WHERE tenant_id=%s AND conversation_id=%s RETURNING id",
                                (tenant,conversation_id)).fetchall())
