import sys
import os
from pathlib import Path
import pymysql
from dotenv import load_dotenv

# 確保中文輸出編碼正確
sys.stdout.reconfigure(encoding='utf-8')
sys.stderr.reconfigure(encoding='utf-8')

# 從專案根目錄的 .env 讀取資料庫連線設定 (若 .env 不存在或缺少某欄位，則回退為原本的預設值)
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

# 資料庫連線配置 (同 import_excel.py，但先不指定 database，因為 sql 檔內含 CREATE DATABASE)
DB_CONFIG = {
    'host': os.getenv('DB_HOST', '127.0.0.1'),
    'port': int(os.getenv('DB_PORT', 3306)),
    'user': os.getenv('DB_USER', 'root'),
    'password': os.getenv('DB_PASSWORD', '1234'),
    'charset': 'utf8mb4'
}


def load_schema_parts(cursor, schema_parts_dir):
    """Execute UTF-8 schema fragments in lexical filename order."""
    parts_dir = Path(schema_parts_dir)
    assert parts_dir.name == "schema_parts" or parts_dir.exists()
    loaded_parts = []
    for part_path in sorted(parts_dir.glob("*.sql"), key=lambda path: path.name):
        sql_content = part_path.read_text(encoding="utf-8")
        statements = []
        current_stmt = []
        for line in sql_content.split("\n"):
            if line.strip().startswith("--"):
                continue
            segments = line.split(";")
            for segment_index, segment in enumerate(segments):
                current_stmt.append(segment)
                if segment_index < len(segments) - 1:
                    statement = "\n".join(current_stmt).strip()
                    if statement:
                        statements.append(statement)
                    current_stmt = []
        trailing = "\n".join(current_stmt).strip()
        if trailing:
            statements.append(trailing)

        try:
            for statement in statements:
                cursor.execute(statement)
        except Exception as exc:
            raise RuntimeError(f"載入 schema part 失敗：{part_path.name}: {exc}") from exc
        loaded_parts.append(part_path.name)
    return loaded_parts

def main():
    schema_path = r'db/schema.sql'
    if not os.path.exists(schema_path):
        # 嘗試相對路徑
        schema_path = os.path.join(os.path.dirname(__file__), '..', 'db', 'schema.sql')
        
    if not os.path.exists(schema_path):
        print(f"錯誤：找不到 schema.sql 檔案，路徑：{schema_path}")
        return

    print(f"正在讀取 {schema_path}...")
    with open(schema_path, 'r', encoding='utf-8') as f:
        sql_content = f.read()

    # 簡單用分號分割 SQL 語句 (排除空行)
    # ponytail: split by semicolon and filter out comments or empty statements
    statements = []
    current_stmt = []
    for line in sql_content.split('\n'):
        # 忽略單行注釋
        if line.strip().startswith('--'):
            continue
        if ';' in line:
            parts = line.split(';')
            current_stmt.append(parts[0])
            statements.append('\n'.join(current_stmt).strip())
            current_stmt = [parts[1]] if len(parts) > 1 else []
        else:
            current_stmt.append(line)
            
    if current_stmt and '\n'.join(current_stmt).strip():
        statements.append('\n'.join(current_stmt).strip())

    print(f"解析出 {len(statements)} 個 SQL 語句，準備執行...")
    
    try:
        connection = pymysql.connect(**DB_CONFIG)
        with connection.cursor() as c:
            c.execute("SET NAMES utf8mb4;")
        print("成功連線至 MySQL 伺服器！")
    except Exception as e:
        print(f"無法連線至 MySQL 伺服器：{e}\n請確認 Docker 中的 MySQL 容器是否已啟動，且連接埠為 3306。")
        return

    success_count = 0
    try:
        with connection.cursor() as cursor:
            for i, stmt in enumerate(statements):
                stmt_clean = stmt.strip()
                if not stmt_clean:
                    continue
                try:
                    cursor.execute(stmt_clean)
                    success_count += 1
                except Exception as stmt_err:
                    print(f"執行第 {i+1} 個語句時出錯：{stmt_err}")
                    print(f"出錯語句：\n{stmt_clean[:200]}...\n")
                    raise stmt_err

            schema_parts_dir = Path(schema_path).parent / "schema_parts"
            loaded_parts = load_schema_parts(cursor, schema_parts_dir)
            if loaded_parts:
                print(f"已依序載入 Schema parts：{', '.join(loaded_parts)}")
            
            connection.commit()
            print(f"\n====== 資料庫 Schema 更新成功！共執行 {success_count} 個語句 ======")
            
            # 預載 2026 年中華民國核心國定假日
            holidays_2026 = [
                ('2026-01-01', '中華民國開國紀念日(元旦)', False),
                ('2026-02-17', '農曆除夕', False),
                ('2026-02-18', '春節初一', False),
                ('2026-02-19', '春節初二', False),
                ('2026-02-20', '春節初三', False),
                ('2026-02-21', '春節初四', False),
                ('2026-02-22', '春節初五', False),
                ('2026-02-27', '和平紀念日(補假)', False),
                ('2026-02-28', '和平紀念日', False),
                ('2026-04-03', '兒童節', False),
                ('2026-04-04', '清明節/民族掃墓節', False),
                ('2026-06-19', '端午節', False),
                ('2026-09-25', '中秋節', False),
                ('2026-10-09', '國慶日(補假)', False),
                ('2026-10-10', '國慶日', False)
            ]
            
            with connection.cursor() as cursor:
                for date_str, name, is_double in holidays_2026:
                    cursor.execute("""
                        INSERT INTO holidays (holiday_date, holiday_name, is_double_pay_default)
                        VALUES (%s, %s, %s)
                        ON DUPLICATE KEY UPDATE holiday_name = %s, is_double_pay_default = %s
                    """, (date_str, name, is_double, name, is_double))
            connection.commit()
            print("====== 2026 年中華民國國定假日預載成功 ======")
            
    except Exception as e:
        connection.rollback()
        print(f"執行失敗，已回滾所有變更。錯誤原因：{e}")
        raise
    finally:
        connection.close()
        print("資料庫連線已關閉。")

if __name__ == '__main__':
    main()
