import uuid
import mysql.connector
from mysql.connector import Error
from datetime import datetime
from decimal import Decimal

import config
from config import DB_CONFIG

# Incremento mínimo entre lances (configurável em config.py: MIN_INCREMENT = 1.0)
MIN_INCREMENT = Decimal(str(getattr(config, "MIN_INCREMENT", 1.0)))
if MIN_INCREMENT < Decimal("0.01"):
    MIN_INCREMENT = Decimal("0.01")

# Limite da coluna DECIMAL(10, 2)
MAX_VALUE = Decimal("100000000")

# Garimpos não têm prazo: só fecham manualmente. A coluna end_time é NOT NULL,
# então guardamos uma data bem distante.
NO_DEADLINE = datetime(2099, 12, 31, 23, 59, 59)


class DatabaseManager:
    def __init__(self):
        self.config = DB_CONFIG
        self._init_db()

    def _get_connection(self):
        return mysql.connector.connect(**self.config)

    def _init_db(self):
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()

                # Migração: se existir a tabela antiga "auctions" e ainda não existir "garimpos",
                # renomeia para preservar o histórico.
                cursor.execute('''
                    SELECT TABLE_NAME FROM information_schema.TABLES
                    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME IN ('auctions', 'garimpos')
                ''')
                existing = {row[0] for row in cursor.fetchall()}
                if 'auctions' in existing and 'garimpos' not in existing:
                    cursor.execute("RENAME TABLE auctions TO garimpos")
                    conn.commit()
                    print("[+] Tabela auctions renomeada para garimpos.")

                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS garimpos (
                        id INT AUTO_INCREMENT PRIMARY KEY,
                        code VARCHAR(20) UNIQUE NOT NULL,
                        card_name VARCHAR(255) NOT NULL,
                        start_price DECIMAL(10, 2) NOT NULL,
                        current_bid DECIMAL(10, 2) NOT NULL,
                        current_winner VARCHAR(255),
                        current_winner_phone VARCHAR(50),
                        end_time DATETIME NOT NULL,
                        status VARCHAR(20) NOT NULL DEFAULT 'ACTIVE',
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                ''')
                conn.commit()

                # Migração: remove a coluna min_increment de bancos antigos, se existir.
                cursor.execute('''
                    SELECT COUNT(*) FROM information_schema.COLUMNS
                    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'garimpos'
                      AND COLUMN_NAME = 'min_increment'
                ''')
                if cursor.fetchone()[0]:
                    cursor.execute("ALTER TABLE garimpos DROP COLUMN min_increment")
                    conn.commit()
                    print("[+] Coluna min_increment removida da tabela garimpos.")
        except Error as e:
            print(f"[-] Erro ao inicializar tabela MySQL: {e}")

    def create_garimpo(self, card_name, start_price):
        """Cria o garimpo e devolve o código (#PKM-<id>), gerado a partir do id do banco."""
        tmp_code = "TMP-" + uuid.uuid4().hex[:12]
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO garimpos (code, card_name, start_price, current_bid, end_time, status)
                VALUES (%s, %s, %s, %s, %s, 'ACTIVE')
            ''', (tmp_code, card_name, start_price, start_price, NO_DEADLINE))
            new_id = cursor.lastrowid
            code = f"#PKM-{new_id:02d}"
            cursor.execute("UPDATE garimpos SET code = %s WHERE id = %s", (code, new_id))
            conn.commit()
        return code

    def get_active_garimpos(self):
        with self._get_connection() as conn:
            cursor = conn.cursor(dictionary=True)
            cursor.execute("SELECT * FROM garimpos WHERE status = 'ACTIVE' ORDER BY id ASC")
            return cursor.fetchall()

    def get_garimpo_by_code(self, code):
        with self._get_connection() as conn:
            cursor = conn.cursor(dictionary=True)
            cursor.execute("SELECT * FROM garimpos WHERE code = %s AND status = 'ACTIVE'", (code.upper(),))
            return cursor.fetchone()

    def place_bid(self, code, bid_value, bidder_name, bidder_phone=None):
        """
        Registra um lance de forma atômica: a validação acontece dentro do próprio UPDATE,
        então dois lances simultâneos nunca se sobrescrevem.
        Regras: 1º lance >= preço inicial; seguintes >= lance atual + incremento mínimo.
        """
        try:
            bid = Decimal(str(bid_value))
        except Exception:
            return False, "Valor de lance inválido."
        if not bid.is_finite() or bid <= 0 or bid >= MAX_VALUE:
            return False, "Valor de lance inválido."
        bid = bid.quantize(Decimal("0.01"))

        code = code.upper()
        with self._get_connection() as conn:
            cursor = conn.cursor(dictionary=True)
            cursor.execute('''
                UPDATE garimpos
                SET current_bid = %s, current_winner = %s, current_winner_phone = %s
                WHERE code = %s AND status = 'ACTIVE'
                  AND (
                        (current_winner IS NULL AND %s >= start_price)
                     OR (current_winner IS NOT NULL AND %s >= current_bid + %s)
                  )
            ''', (bid, bidder_name, bidder_phone, code, bid, bid, MIN_INCREMENT))
            conn.commit()

            if cursor.rowcount > 0:
                return True, {"current_bid": bid}

            # Não aceitou: descobre o motivo com o estado atual.
            cursor.execute("SELECT * FROM garimpos WHERE code = %s AND status = 'ACTIVE'", (code,))
            garimpo = cursor.fetchone()

        if not garimpo:
            return False, "Garimpo não encontrado ou já encerrado."

        if garimpo['current_winner']:
            needed = garimpo['current_bid'] + MIN_INCREMENT
            return False, (
                f"Lance inválido. O lance mínimo agora é R$ {needed:.2f} "
                f"(atual R$ {garimpo['current_bid']:.2f} + incremento de R$ {MIN_INCREMENT:.2f})."
            )
        return False, f"Lance inválido. O lance mínimo é o preço inicial (R$ {garimpo['start_price']:.2f})."

    def force_close_garimpo(self, code):
        """Encerra manualmente um garimpo ativo. Retorna True se algum garimpo foi encerrado."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE garimpos SET status = 'CLOSED', end_time = %s "
                "WHERE code = %s AND status = 'ACTIVE'",
                (datetime.now(), code.upper()),
            )
            conn.commit()
            return cursor.rowcount > 0

    def close_all_garimpos(self):
        """Encerra todos os garimpos ativos de uma vez. Retorna a lista do que foi encerrado."""
        with self._get_connection() as conn:
            cursor = conn.cursor(dictionary=True)
            cursor.execute("SELECT * FROM garimpos WHERE status = 'ACTIVE' ORDER BY id ASC")
            active = cursor.fetchall()
            if active:
                cursor.execute(
                    "UPDATE garimpos SET status = 'CLOSED', end_time = %s WHERE status = 'ACTIVE'",
                    (datetime.now(),),
                )
                conn.commit()
            return active

    def cancel_garimpo(self, code):
        """Cancela um garimpo ativo (some do relatório de vendas). Retorna True se cancelou."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE garimpos SET status = 'CANCELLED', end_time = %s "
                "WHERE code = %s AND status = 'ACTIVE'",
                (datetime.now(), code.upper()),
            )
            conn.commit()
            return cursor.rowcount > 0

    def get_recent_garimpos(self, limit=10):
        """Últimos garimpos criados (ativos, encerrados e cancelados), do mais novo para o mais antigo."""
        with self._get_connection() as conn:
            cursor = conn.cursor(dictionary=True)
            cursor.execute("SELECT * FROM garimpos ORDER BY id DESC LIMIT %s", (int(limit),))
            return cursor.fetchall()

    def get_my_garimpos(self, name, phone=None, limit=15):
        """Garimpos (ativos e encerrados) em que a pessoa é o maior lance."""
        with self._get_connection() as conn:
            cursor = conn.cursor(dictionary=True)
            cursor.execute('''
                SELECT * FROM garimpos
                WHERE status IN ('ACTIVE', 'CLOSED')
                  AND (current_winner = %s
                       OR (current_winner_phone IS NOT NULL AND current_winner_phone = %s))
                ORDER BY id DESC LIMIT %s
            ''', (name, phone or "", int(limit)))
            return cursor.fetchall()

    def get_sales_report(self):
        with self._get_connection() as conn:
            cursor = conn.cursor(dictionary=True)
            cursor.execute("SELECT * FROM garimpos WHERE status = 'CLOSED' ORDER BY end_time DESC")
            return cursor.fetchall()