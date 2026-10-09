import os
import sys
import re
import time
import threading
import logging
from datetime import datetime
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

import config
from config import ADMIN_PHONE, TARGET_GROUP_NAME, PROFILE_DIR
from database import DatabaseManager

# Silencia avisos de tentativa de reconexão do urllib3 e Selenium
logging.getLogger("urllib3").setLevel(logging.ERROR)
logging.getLogger("selenium").setLevel(logging.ERROR)

# Configuração do sistema de Log (Salva em bot.log e exibe no console)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler("bot.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)

ADMIN_NAMES = [n.strip().lower() for n in getattr(config, "ADMIN_NAMES", [])]

READ_LAST_N = 15
PROCESS_BACKLOG = False

FOOTER_INPUT_XPATH = "(//footer//div[@contenteditable='true'])[1]"

SEARCH_INPUT_XPATH = (
    "//input[@role='textbox' and @data-tab='3'] | "
    "//div[@contenteditable='true'][@data-tab='3']"
)

JS_READ_MESSAGES = """
const limit = arguments[0];
const seen = new Set();
const out = [];
document.querySelectorAll('[data-pre-plain-text]').forEach(copy => {
  const holder = copy.closest('[data-id]');
  const pre = copy.getAttribute('data-pre-plain-text') || '';
  const node = copy.querySelector('.selectable-text') || copy.querySelector('[data-testid="selectable-text"]');
  const text = (node ? (node.innerText || node.textContent || '') : '').trim();
  const id = holder ? holder.getAttribute('data-id') : ('pre:' + pre + '|' + text);
  if (seen.has(id)) return;
  seen.add(id);
  const scope = holder || copy.parentElement;
  let fromMe = false;
  const meta = scope ? scope.querySelector('[data-testid="msg-meta"]') : null;
  if (meta) {
    fromMe = Array.from(meta.querySelectorAll('svg title'))
      .some(t => /^(wds-ic-(read|delivered|sent|time|pending)|msg-)/.test((t.textContent || '').trim()));
  }
  if (!fromMe && scope && scope.querySelector('[aria-label="Você:"], [aria-label="You:"]')) fromMe = true;
  out.push({id: id, fromMe: fromMe, pre: pre, text: text});
});
return out.slice(-limit);
"""

JS_INSERT_TEXT = """
const el = arguments[0];
el.focus();
return document.execCommand('insertText', false, arguments[1]);
"""


def digits(value):
    return re.sub(r"\D", "", value or "")


def normalize_phone(value):
    d = digits(value)
    if d.startswith("55") and len(d) == 13 and d[4] == "9":
        d = d[:4] + d[5:]
    return d


def xpath_literal(s):
    if "'" not in s:
        return f"'{s}'"
    if '"' not in s:
        return f'"{s}"'
    return "concat(" + ", \"'\", ".join(f"'{p}'" for p in s.split("'")) + ")"


def parse_sender(pre_plain_text):
    m = re.match(r"^\[[^\]]*\]\s*(.*?):\s*$", pre_plain_text or "", re.S)
    name = m.group(1).strip() if m else ""
    return name or "Desconhecido"


def is_admin(sender, from_me=False):
    if from_me:
        return True
    admin_phone = normalize_phone(ADMIN_PHONE)
    if admin_phone and normalize_phone(sender) == admin_phone:
        return True
    return sender.strip().lower() in ADMIN_NAMES


class WhatsAppAuctionBot:
    def __init__(self):
        self.target_group = TARGET_GROUP_NAME
        self.admin_phone = ADMIN_PHONE
        self.db = DatabaseManager()
        self.driver_lock = threading.RLock()
        self.driver = self._init_driver()
        self.processed_msg_ids = set()
        self.running = True

    def _init_driver(self):
        chrome_options = Options()
        chrome_options.add_argument(f"--user-data-dir={PROFILE_DIR}")
        chrome_options.add_argument("--no-sandbox")
        chrome_options.add_argument("--disable-dev-shm-usage")

        driver = webdriver.Chrome(options=chrome_options)
        driver.maximize_window()
        return driver

    def start(self):
        logging.info("Abrindo WhatsApp Web...")
        self.driver.get("https://web.whatsapp.com")

        logging.info("Aguardando carregamento...")

        search_xpath = "//div[@id='pane-side'] | //div[@contenteditable='true'] | //*[@data-tab='3']"

        try:
            WebDriverWait(self.driver, 120).until(
                EC.presence_of_element_located((By.XPATH, search_xpath))
            )
            logging.info("WhatsApp Web carregado com sucesso!")
            time.sleep(3)
        except Exception as e:
            logging.error(f"Timeout no carregamento: {e}")
            return

        if not self._select_chat(self.target_group):
            self.stop_bot()
            return

        if not PROCESS_BACKLOG:
            self._prime_backlog()

        logging.info("Bot de Leilão iniciado e escutando comandos...")
        self._listen_messages()

    def stop_bot(self):
        logging.info("Encerrando bot...")
        self.running = False

        with self.driver_lock:
            if self.driver:
                try:
                    self.driver.close()
                except Exception:
                    pass
                try:
                    self.driver.quit()
                except Exception:
                    pass

        logging.info("Bot finalizado com sucesso.")
        os._exit(0)

    def _chat_is_open(self, chat_name):
        try:
            with self.driver_lock:
                xp = (
                    f"//*[@id='main']//header//span"
                    f"[contains(@title, {xpath_literal(chat_name)}) "
                    f"or contains(normalize-space(.), {xpath_literal(chat_name)})]"
                )
                return len(self.driver.find_elements(By.XPATH, xp)) > 0
        except Exception:
            return False

    def _select_chat(self, chat_name):
        if self._chat_is_open(chat_name):
            logging.info(f"Grupo '{chat_name}' já está aberto.")
            return True

        try:
            with self.driver_lock:
                box = WebDriverWait(self.driver, 15).until(
                    EC.presence_of_element_located((By.XPATH, SEARCH_INPUT_XPATH))
                )
                self.driver.execute_script("arguments[0].focus(); arguments[0].click();", box)
                box.send_keys(Keys.COMMAND, "a")
                box.send_keys(Keys.BACKSPACE)
                box.send_keys(chat_name)

            time.sleep(2.5)  # espera os resultados renderizarem

            with self.driver_lock:
                box = self.driver.find_element(By.XPATH, SEARCH_INPUT_XPATH)
                box.send_keys(Keys.ARROW_DOWN)  # vai para o 1º resultado
                time.sleep(0.5)
                box.send_keys(Keys.ENTER)       # abre a conversa

            time.sleep(2)

            if self._chat_is_open(chat_name):
                logging.info(f"Grupo '{chat_name}' aberto com sucesso.")
                return True
        except Exception as e:
            logging.error(f"Falha ao selecionar '{chat_name}': {e!r}")

        logging.error(
            f"Não foi possível abrir o grupo '{chat_name}'. "
            f"Confira TARGET_GROUP_NAME no config.py (acentos, emojis, espaços)."
        )
        return False

    def send_message(self, text):
        try:
            with self.driver_lock:
                input_box = WebDriverWait(self.driver, 10).until(
                    EC.presence_of_element_located((By.XPATH, FOOTER_INPUT_XPATH))
                )
                self.driver.execute_script("arguments[0].focus();", input_box)
                lines = text.split('\n')
                for i, line in enumerate(lines):
                    if line:
                        ok = self.driver.execute_script(JS_INSERT_TEXT, input_box, line)
                        if not ok:
                            input_box.send_keys(line)
                    if i < len(lines) - 1:
                        input_box.send_keys(Keys.SHIFT, Keys.ENTER)

                input_box.send_keys(Keys.ENTER)
                logging.info(f"[📩 enviada] Conteúdo: '{text[:50]}...'")
        except Exception as e:
            logging.error(f"Erro ao enviar mensagem: {e!r}")

    def _read_rows(self):
        with self.driver_lock:
            return self.driver.execute_script(JS_READ_MESSAGES, READ_LAST_N) or []

    def _prime_backlog(self):
        rows = []
        for _ in range(5):
            rows = self._read_rows()
            if rows:
                break
            time.sleep(1)
        for r in rows:
            self.processed_msg_ids.add(r["id"])

    def _listen_messages(self):
        logging.info("Monitorando mensagens...")
        while self.running:
            try:
                rows = self._read_rows()

                for r in rows:
                    msg_id, text = r["id"], r["text"]
                    if not text or msg_id in self.processed_msg_ids:
                        continue

                    self.processed_msg_ids.add(msg_id)
                    sender = parse_sender(r["pre"])
                    origem = "enviada" if r["fromMe"] else "recebida"

                    logging.info(f"[📩 {origem}] De: {sender} | Conteúdo: '{text}'")

                    if not text.startswith("!"):
                        continue

                    self._process_command(text, sender, r["fromMe"])

            except Exception as e:
                logging.error(f"Erro na leitura de mensagens: {e!r}")
                time.sleep(2)

            time.sleep(1.5)

    def _process_command(self, text, sender, from_me=False):
        args = text.split()
        if not args:
            return

        cmd = args[0].lower()
        admin = is_admin(sender, from_me)

        # 1. CRIAR LEILÃO (Sintaxe: !criar_leilao Nome do Item | Preço Inicial)
        if cmd == "!criar_leilao":
            if not admin:
                self.send_message("❌ Apenas o administrador pode criar leilões.")
                return

            try:
                payload = text.replace("!criar_leilao", "").strip()
                parts = [p.strip() for p in payload.split("|")]

                if len(parts) < 2:
                    raise ValueError("Formato incorreto.")

                item_name, start_price = parts[0], float(parts[1].replace(",", "."))

                count = len(self.db.get_sales_report()) + len(self.db.get_active_auctions()) + 1
                code = f"#PKM-{count:02d}"

                # Regista o leilão na base de dados
                self.db.create_auction(code, item_name, start_price, 999999)

                msg = (
                    f"🔥 **LEILÃO REGISTADO!** 🔥\n\n"
                    f"🏷️ **Código:** {code}\n"
                    f"📦 **Item:** {item_name}\n"
                    f"💰 **Preço Inicial:** R$ {start_price:.2f}\n\n"
                    f"👉 Para dar um lance, envie: `!lance {code} <valor>`"
                )
                self.send_message(msg)
            except Exception as e:
                logging.error(f"Falha em !criar_leilao: {e!r}")
                self.send_message("⚠️ Sintaxe incorreta. Use: `!criar_leilao [Nome do Item] | [Preço Inicial]`")

        # 2. ENCERRAR LEILÃO MANUALMENTE
        elif cmd in ["!encerrar_leilao", "!fechar_leilao", "!encerrar"]:
            if not admin:
                self.send_message("❌ Apenas o administrador pode encerrar um leilão.")
                return

            try:
                code = args[1].upper()
                auctions = self.db.get_active_auctions()
                leilao_alvo = next((a for a in auctions if a['code'] == code), None)

                if not leilao_alvo:
                    self.send_message(f"⚠️ Nenhum leilão ativo encontrado com o código **{code}**.")
                    return

                self.db.force_close_auction(code)

                if leilao_alvo['current_winner']:
                    msg = (
                        f"🎉 **LEILÃO ENCERRADO!** 🎉\n\n"
                        f"🏷️ **Código:** {leilao_alvo['code']}\n"
                        f"📦 **Item:** {leilao_alvo['card_name']}\n"
                        f"🏆 **Vencedor:** {leilao_alvo['current_winner']}\n"
                        f"💵 **Valor Final:** R$ {leilao_alvo['current_bid']:.2f}"
                    )
                else:
                    msg = f"🔔 **LEILÃO ENCERRADO SEM LANCES** 🔔\n\nO leilão **{leilao_alvo['code']}** foi encerrado sem lances."

                self.send_message(msg)
            except IndexError:
                self.send_message("⚠️ Informe o código do leilão. Exemplo: `!encerrar_leilao #PKM-01`")

        # 3. DAR LANCE
        elif cmd == "!lance":
            try:
                code, bid_value = args[1].upper(), float(args[2].replace(",", "."))
                success, result = self.db.place_bid(code, bid_value, sender, sender)

                if success:
                    msg = f"✅ **LANCE ACEITO!**\n\nO usuário **{sender}** deu um lance de **R$ {bid_value:.2f}** para o leilão **{code}**."
                    self.send_message(msg)
                else:
                    self.send_message(f"❌ {result}")
            except (IndexError, ValueError):
                self.send_message("⚠️ Formato incorreto. Use: `!lance #PKM-01 150`")

        # 4. EXIBIR LEILÕES ATIVOS
        elif cmd in ["!catalogo", "!leiloes"]:
            auctions = self.db.get_active_auctions()
            if not auctions:
                self.send_message("📭 Nenhum leilão ativo no momento.")
                return

            msg = "📋 **LEILÕES ATIVOS NO MOMENTO** 📋\n\n"
            for a in auctions:
                msg += (
                    f"🔹 **Código:** {a['code']}\n"
                    f"   Item: {a['card_name']}\n"
                    f"   Lance Atual: R$ {a['current_bid']:.2f} ({a['current_winner'] or 'Sem lances'})\n"
                    f"-----------------------------\n"
                )
            self.send_message(msg)

        # 5. RELATÓRIO DE VENDAS
        elif cmd == "!relatorio":
            if not admin:
                return

            report = self.db.get_sales_report()
            if not report:
                self.send_message("📊 Nenhum leilão finalizado no histórico.")
                return

            msg = "📊 **RELATÓRIO DE VENDAS ENCERRADAS** 📊\n\n"
            for item in report:
                vencedor = item['current_winner'] or "Sem lances"
                msg += f"• **{item['code']}** - {item['card_name']} | Arremate: R$ {item['current_bid']:.2f} | Comprador: {vencedor}\n"
            self.send_message(msg)


if __name__ == "__main__":
    bot = WhatsAppAuctionBot()
    try:
        bot.start()
    except KeyboardInterrupt:
        bot.stop_bot()