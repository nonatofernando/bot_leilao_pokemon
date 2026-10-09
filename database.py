import mysql.connector
from mysql.connector import Error
from datetime import datetime, timedelta
from config import DB_CONFIG

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
                cursor.execute('''
                    CREATE TABLE IF NOT EXISTS auctions (
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

                # Migração: bancos criados antes ainda têm a coluna min_increment (NOT NULL, sem
                # padrão), o que faria o INSERT novo falhar. Remove a coluna se existir.
                cursor.execute('''
                    SELECT COUNT(*) FROM information_schema.COLUMNS
                    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'auctions'
                      AND COLUMN_NAME = 'min_increment'
                ''')
                if cursor.fetchone()[0]:
                    cursor.execute("ALTER TABLE auctions DROP COLUMN min_increment")
                    conn.commit()
                    print("[+] Coluna min_increment removida da tabela auctions.")
        except Error as e:
            print(f"[-] Erro ao inicializar tabela MySQL: {e}")

    def create_auction(self, code, card_name, start_price, duration_minutes):
        end_time = datetime.now() + timedelta(minutes=duration_minutes)
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO auctions (code, card_name, start_price, current_bid, end_time, status)
                VALUES (%s, %s, %s, %s, %s, 'ACTIVE')
            ''', (code, card_name, start_price, start_price, end_time))
            conn.commit()

    def get_active_auctions(self):
        with self._get_connection() as conn:
            cursor = conn.cursor(dictionary=True)
            cursor.execute("SELECT * FROM auctions WHERE status = 'ACTIVE' ORDER BY end_time ASC")
            return cursor.fetchall()

    def get_auction_by_code(self, code):
        with self._get_connection() as conn:
            cursor = conn.cursor(dictionary=True)
            cursor.execute("SELECT * FROM auctions WHERE code = %s AND status = 'ACTIVE'", (code.upper(),))
            return cursor.fetchone()

    def place_bid(self, code, bid_value, bidder_name, bidder_phone):
        auction = self.get_auction_by_code(code)
        if not auction:
            return False, "Leilão não encontrado ou já encerrado."

        # 1º lance: pelo menos o preço inicial. Depois: precisa superar o lance atual.
        if auction['current_winner']:
            if bid_value <= auction['current_bid']:
                return False, f"Lance inválido. O lance precisa ser maior que o atual (R$ {auction['current_bid']:.2f})."
        elif bid_value < auction['start_price']:
            return False, f"Lance inválido. O lance mínimo é o preço inicial (R$ {auction['start_price']:.2f})."

        now = datetime.now()
        end_time = auction['end_time']

        time_left = (end_time - now).total_seconds()
        extended = False
        if time_left <= 120:
            end_time = end_time + timedelta(minutes=2)
            extended = True

        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE auctions
                SET current_bid = %s, current_winner = %s, current_winner_phone = %s, end_time = %s
                WHERE code = %s
            ''', (bid_value, bidder_name, bidder_phone, end_time, code.upper()))
            conn.commit()

        return True, {"current_bid": bid_value, "extended": extended, "new_end_time": end_time}

    def force_close_auction(self, code):
        """Encerra manualmente um leilão ativo. Retorna True se algum leilão foi encerrado."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE auctions SET status = 'CLOSED', end_time = %s "
                "WHERE code = %s AND status = 'ACTIVE'",
                (datetime.now(), code.upper()),
            )
            conn.commit()
            return cursor.rowcount > 0

    def close_expired_auctions(self):
        now = datetime.now()
        with self._get_connection() as conn:
            cursor = conn.cursor(dictionary=True)
            cursor.execute("SELECT * FROM auctions WHERE status = 'ACTIVE' AND end_time <= %s", (now,))
            expired = cursor.fetchall()

            for a in expired:
                cursor.execute("UPDATE auctions SET status = 'CLOSED' WHERE id = %s", (a['id'],))
            conn.commit()
        return expired

    def get_sales_report(self):
        with self._get_connection() as conn:
            cursor = conn.cursor(dictionary=True)
            cursor.execute("SELECT * FROM auctions WHERE status = 'CLOSED' ORDER BY end_time DESC")
            return cursor.fetchall()