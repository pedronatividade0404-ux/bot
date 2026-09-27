import io
import os
import re
import asyncio
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands
from discord.ui import Modal, TextInput, View, button
from dotenv import load_dotenv
import aiosqlite

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
WEBHOOK_URL = os.getenv("PROOF_WEBHOOK_URL", "").strip()
REVIEW_CHANNEL_ID = int(os.getenv("REVIEW_CHANNEL_ID", "0") or 0)
ADMIN_ROLE_ID = int(os.getenv("ADMIN_ROLE_ID", "0") or 0)
GUILD_ID = int(os.getenv("GUILD_ID", "0") or 0)
TIKTOK_LITE_LINK = os.getenv("TIKTOK_LITE_LINK", "").strip()
DB_PATH = os.getenv("DB_PATH", "data/bot.db").strip()
PROOF_TIMEOUT_SECONDS = int(os.getenv("PROOF_TIMEOUT_SECONDS", "180") or 180)
MAX_PROOF_MB = int(os.getenv("MAX_PROOF_MB", "10") or 10)
REWARD_CENTS = int(os.getenv("REWARD_CENTS", "500") or 500)
MIN_WITHDRAWAL_CENTS = int(os.getenv("MIN_WITHDRAWAL_CENTS", "1000") or 1000)

os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def display_dt(value: str) -> str:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(ZoneInfo("America/Sao_Paulo")).strftime("%d/%m/%Y - %H:%M")


def money(cents: int) -> str:
    return f"R$ {cents / 100:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def parse_money(value: str) -> Optional[int]:
    text = value.strip().lower().replace("r$", "").replace(" ", "")
    if not text:
        return None
    # Brazilian format: 10,50 / 1.000,50; also accept 10.50.
    try:
        if "," in text:
            text = text.replace(".", "").replace(",", ".")
        amount = Decimal(text).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        cents = int(amount * 100)
        return cents if cents >= 0 else None
    except (InvalidOperation, ValueError):
        return None


def is_image_attachment(attachment: discord.Attachment) -> bool:
    content_type = (attachment.content_type or "").lower()
    if content_type.startswith("image/"):
        return True
    return bool(re.search(r"\.(png|jpe?g|gif|webp)$", attachment.filename or "", re.I))


class Database:
    def __init__(self, path: str):
        self.path = path

    async def init(self):
        async with aiosqlite.connect(self.path) as db:
            await db.executescript(
                """
                PRAGMA journal_mode=WAL;
                PRAGMA foreign_keys=ON;

                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    balance_cents INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS proofs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL,
                    reviewed_at TEXT,
                    reviewer_id INTEGER,
                    rejection_reason TEXT,
                    review_message_id INTEGER,
                    review_channel_id INTEGER,
                    webhook_sent INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS withdrawals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    amount_cents INTEGER NOT NULL,
                    pix_type TEXT NOT NULL,
                    recipient_name TEXT NOT NULL,
                    pix_key TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL,
                    updated_at TEXT,
                    processed_by INTEGER
                );

                CREATE TABLE IF NOT EXISTS transactions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    amount_cents INTEGER NOT NULL,
                    type TEXT NOT NULL,
                    description TEXT NOT NULL,
                    reference_type TEXT,
                    reference_id INTEGER,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_proofs_user ON proofs(user_id);
                CREATE INDEX IF NOT EXISTS idx_proofs_status ON proofs(status);
                CREATE INDEX IF NOT EXISTS idx_withdrawals_user ON withdrawals(user_id);
                CREATE INDEX IF NOT EXISTS idx_withdrawals_status ON withdrawals(status);
                CREATE INDEX IF NOT EXISTS idx_transactions_user ON transactions(user_id);
                """
            )
            await db.commit()

    async def ensure_user(self, user_id: int):
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT OR IGNORE INTO users(user_id, balance_cents, created_at) VALUES (?, 0, ?)",
                (user_id, now_iso()),
            )
            await db.commit()

    async def get_balance(self, user_id: int) -> int:
        await self.ensure_user(user_id)
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute("SELECT balance_cents FROM users WHERE user_id = ?", (user_id,))
            row = await cur.fetchone()
            return int(row[0] if row else 0)

    async def count_user_proofs(self, user_id: int, status: Optional[str] = None) -> int:
        async with aiosqlite.connect(self.path) as db:
            if status:
                cur = await db.execute(
                    "SELECT COUNT(*) FROM proofs WHERE user_id = ? AND status = ?", (user_id, status)
                )
            else:
                cur = await db.execute("SELECT COUNT(*) FROM proofs WHERE user_id = ?", (user_id,))
            row = await cur.fetchone()
            return int(row[0])

    async def has_pending_proof(self, user_id: int) -> bool:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute("SELECT 1 FROM proofs WHERE user_id = ? AND status = 'pending' LIMIT 1", (user_id,))
            return await cur.fetchone() is not None

    async def create_proof(self, user_id: int) -> int:
        await self.ensure_user(user_id)
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "INSERT INTO proofs(user_id, status, created_at) VALUES (?, 'pending', ?)",
                (user_id, now_iso()),
            )
            await db.commit()
            return int(cur.lastrowid)

    async def set_proof_messages(self, proof_id: int, channel_id: int, message_id: int, webhook_sent: bool):
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "UPDATE proofs SET review_channel_id = ?, review_message_id = ?, webhook_sent = ? WHERE id = ?",
                (channel_id, message_id, 1 if webhook_sent else 0, proof_id),
            )
            await db.commit()

    async def get_proof(self, proof_id: int):
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute("SELECT * FROM proofs WHERE id = ?", (proof_id,))
            return await cur.fetchone()

    async def pending_proof_rows(self):
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "SELECT id, user_id, review_channel_id, review_message_id FROM proofs WHERE status = 'pending'"
            )
            return await cur.fetchall()

    async def approve_proof(self, proof_id: int, reviewer_id: int):
        async with aiosqlite.connect(self.path) as db:
            await db.execute("BEGIN IMMEDIATE")
            cur = await db.execute("SELECT user_id, status FROM proofs WHERE id = ?", (proof_id,))
            row = await cur.fetchone()
            if not row:
                await db.rollback()
                return False, "Prova não encontrada.", None
            user_id, status = int(row[0]), row[1]
            if status != "pending":
                await db.rollback()
                return False, f"Esta prova já foi {status}.", user_id

            await db.execute(
                "UPDATE proofs SET status='approved', reviewed_at=?, reviewer_id=? WHERE id=?",
                (now_iso(), reviewer_id, proof_id),
            )
            await db.execute("UPDATE users SET balance_cents = balance_cents + ? WHERE user_id = ?", (REWARD_CENTS, user_id))
            await db.execute(
                "INSERT INTO transactions(user_id, amount_cents, type, description, reference_type, reference_id, created_at) VALUES (?, ?, 'credit', ?, 'proof', ?, ?)",
                (user_id, REWARD_CENTS, f"Prova #{proof_id} aprovada", proof_id, now_iso()),
            )
            await db.commit()
            return True, f"Prova aprovada. {money(REWARD_CENTS)} adicionados.", user_id

    async def reject_proof(self, proof_id: int, reviewer_id: int, reason: str):
        async with aiosqlite.connect(self.path) as db:
            await db.execute("BEGIN IMMEDIATE")
            cur = await db.execute("SELECT user_id, status FROM proofs WHERE id = ?", (proof_id,))
            row = await cur.fetchone()
            if not row:
                await db.rollback()
                return False, "Prova não encontrada.", None
            user_id, status = int(row[0]), row[1]
            if status != "pending":
                await db.rollback()
                return False, f"Esta prova já foi {status}.", user_id
            await db.execute(
                "UPDATE proofs SET status='rejected', reviewed_at=?, reviewer_id=?, rejection_reason=? WHERE id=?",
                (now_iso(), reviewer_id, reason[:500], proof_id),
            )
            await db.commit()
            return True, "Prova reprovada.", user_id

    async def create_withdrawal(self, user_id: int, amount_cents: int, pix_type: str, recipient_name: str, pix_key: str):
        await self.ensure_user(user_id)
        async with aiosqlite.connect(self.path) as db:
            await db.execute("BEGIN IMMEDIATE")
            cur = await db.execute("SELECT balance_cents FROM users WHERE user_id = ?", (user_id,))
            row = await cur.fetchone()
            balance = int(row[0]) if row else 0
            if amount_cents < MIN_WITHDRAWAL_CENTS:
                await db.rollback()
                return False, "O valor mínimo para saque é R$ 10,00.", None
            if amount_cents > balance:
                await db.rollback()
                return False, "Saldo insuficiente.", None
            cur = await db.execute(
                "INSERT INTO withdrawals(user_id, amount_cents, pix_type, recipient_name, pix_key, status, created_at) VALUES (?, ?, ?, ?, ?, 'pending', ?)",
                (user_id, amount_cents, pix_type, recipient_name[:150], pix_key[:200], now_iso()),
            )
            withdrawal_id = int(cur.lastrowid)
            await db.execute("UPDATE users SET balance_cents = balance_cents - ? WHERE user_id = ?", (amount_cents, user_id))
            await db.execute(
                "INSERT INTO transactions(user_id, amount_cents, type, description, reference_type, reference_id, created_at) VALUES (?, ?, 'debit', ?, 'withdrawal', ?, ?)",
                (user_id, -amount_cents, f"Saque #{withdrawal_id} solicitado", withdrawal_id, now_iso()),
            )
            await db.commit()
            return True, "Saque criado com sucesso.", withdrawal_id

    async def list_withdrawals(self, user_id: int, limit: int = 20):
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "SELECT id, amount_cents, status, created_at, updated_at, pix_type, recipient_name, pix_key FROM withdrawals WHERE user_id = ? ORDER BY id DESC LIMIT ?",
                (user_id, limit),
            )
            return await cur.fetchall()

    async def pending_withdrawals(self, limit: int = 20):
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "SELECT id, user_id, amount_cents, pix_type, recipient_name, pix_key, status, created_at FROM withdrawals WHERE status='pending' ORDER BY id ASC LIMIT ?",
                (limit,),
            )
            return await cur.fetchall()

    async def update_withdrawal_status(self, withdrawal_id: int, status: str, admin_id: int):
        if status not in {"completed", "cancelled"}:
            return False, "Status inválido.", None
        async with aiosqlite.connect(self.path) as db:
            await db.execute("BEGIN IMMEDIATE")
            cur = await db.execute("SELECT user_id, amount_cents, status FROM withdrawals WHERE id = ?", (withdrawal_id,))
            row = await cur.fetchone()
            if not row:
                await db.rollback()
                return False, "Saque não encontrado.", None
            user_id, amount_cents, old_status = int(row[0]), int(row[1]), row[2]
            if old_status != "pending":
                await db.rollback()
                return False, f"Saque já está {old_status}.", user_id
            await db.execute(
                "UPDATE withdrawals SET status=?, updated_at=?, processed_by=? WHERE id=?",
                (status, now_iso(), admin_id, withdrawal_id),
            )
            if status == "cancelled":
                await db.execute("UPDATE users SET balance_cents = balance_cents + ? WHERE user_id = ?", (amount_cents, user_id))
                await db.execute(
                    "INSERT INTO transactions(user_id, amount_cents, type, description, reference_type, reference_id, created_at) VALUES (?, ?, 'credit', ?, 'withdrawal', ?, ?)",
                    (user_id, amount_cents, f"Saque #{withdrawal_id} cancelado e saldo devolvido", withdrawal_id, now_iso()),
                )
            await db.commit()
            return True, f"Saque {status}.", user_id

    async def admin_adjust_balance(self, user_id: int, amount_cents: int, admin_id: int, reason: str):
        await self.ensure_user(user_id)
        async with aiosqlite.connect(self.path) as db:
            await db.execute("BEGIN IMMEDIATE")
            if amount_cents < 0:
                cur = await db.execute("SELECT balance_cents FROM users WHERE user_id = ?", (user_id,))
                balance = int((await cur.fetchone())[0])
                if balance + amount_cents < 0:
                    await db.rollback()
                    return False, "Ajuste deixaria o saldo negativo."
            await db.execute("UPDATE users SET balance_cents = balance_cents + ? WHERE user_id = ?", (amount_cents, user_id))
            await db.execute(
                "INSERT INTO transactions(user_id, amount_cents, type, description, reference_type, reference_id, created_at) VALUES (?, ?, 'admin_adjustment', ?, 'admin', ?, ?)",
                (user_id, amount_cents, f"Ajuste administrativo: {reason[:300]}", admin_id, now_iso()),
            )
            await db.commit()
            return True, "Saldo ajustado."

    async def stats(self):
        async with aiosqlite.connect(self.path) as db:
            queries = {
                "users": "SELECT COUNT(*) FROM users",
                "pending_proofs": "SELECT COUNT(*) FROM proofs WHERE status='pending'",
                "approved_proofs": "SELECT COUNT(*) FROM proofs WHERE status='approved'",
                "pending_withdrawals": "SELECT COUNT(*) FROM withdrawals WHERE status='pending'",
                "completed_withdrawals": "SELECT COALESCE(SUM(amount_cents),0) FROM withdrawals WHERE status='completed'",
            }
            out = {}
            for key, q in queries.items():
                cur = await db.execute(q)
                out[key] = int((await cur.fetchone())[0])
            return out


async def send_webhook_proof(bot_instance: "Bot", proof_id: int, user: discord.User, attachment: discord.Attachment) -> bool:
    if not WEBHOOK_URL:
        return False
    try:
        data = await attachment.read()
        webhook = discord.Webhook.from_url(WEBHOOK_URL, client=bot_instance)
        embed = discord.Embed(title=f"📸 Nova prova #{proof_id}", color=discord.Color.blurple())
        embed.add_field(name="Usuário", value=f"{user.mention} (`{user.id}`)", inline=False)
        embed.add_field(name="Recompensa", value=money(REWARD_CENTS), inline=True)
        embed.add_field(name="Status", value="Pendente de análise", inline=True)
        embed.set_footer(text="Use o painel de revisão enviado pelo bot para aprovar/reprovar.")
        await webhook.send(
            embed=embed,
            file=discord.File(io.BytesIO(data), filename=attachment.filename),
            username="TikTok Lite Rewards",
        )
        return True
    except Exception:
        return False


class Bot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)
        self.db = Database(DB_PATH)

    async def setup_hook(self):
        await self.db.init()
        self.add_view(RewardsPanelView(self))
        if REVIEW_CHANNEL_ID:
            for row in await self.db.pending_proof_rows():
                proof_id, user_id, channel_id, message_id = row
                if channel_id and message_id:
                    self.add_view(ReviewView(self, proof_id), message_id=message_id)

        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            print(f"Slash commands sincronizados na guild {GUILD_ID}: {len(synced)}")
        else:
            synced = await self.tree.sync()
            print(f"Slash commands globais sincronizados: {len(synced)}")

    async def on_ready(self):
        print(f"Bot online: {self.user} | ID {self.user.id}")


bot = Bot()


def is_admin(member: discord.Member) -> bool:
    if member.guild_permissions.administrator:
        return True
    return bool(ADMIN_ROLE_ID and any(role.id == ADMIN_ROLE_ID for role in member.roles))


def admin_check(interaction: discord.Interaction) -> bool:
    return isinstance(interaction.user, discord.Member) and is_admin(interaction.user)


class PanelView(View):
    def __init__(self, bot_instance: Bot):
        super().__init__(timeout=None)
        self.bot_instance = bot_instance

    @button(label="Enviar provas", emoji="📸", style=discord.ButtonStyle.primary, custom_id="rewards:proof")
    async def proof(self, interaction: discord.Interaction, button: discord.ui.Button):
        if await self.bot_instance.db.has_pending_proof(interaction.user.id):
            await interaction.response.send_message("⏳ Você já possui uma prova pendente de análise.", ephemeral=True)
            return
        await interaction.response.send_message(
            f"📸 Agora envie **uma imagem** neste canal. Você tem {PROOF_TIMEOUT_SECONDS} segundos.\n\n"
            "A imagem precisa ser a comprovação do convite/atividade. Não envie dados sensíveis de terceiros.",
            ephemeral=True,
        )

        def check(message: discord.Message):
            return (
                message.author.id == interaction.user.id
                and message.channel.id == interaction.channel_id
                and len(message.attachments) > 0
                and any(is_image_attachment(a) for a in message.attachments)
            )

        try:
            message = await self.bot_instance.wait_for("message", timeout=PROOF_TIMEOUT_SECONDS, check=check)
        except asyncio.TimeoutError:
            try:
                await interaction.followup.send("⌛ Tempo esgotado. Clique novamente em **Enviar provas** para tentar de novo.", ephemeral=True)
            except discord.HTTPException:
                pass
            return

        attachment = next(a for a in message.attachments if is_image_attachment(a))
        if attachment.size > MAX_PROOF_MB * 1024 * 1024:
            await interaction.followup.send(f"❌ A imagem excede o limite de {MAX_PROOF_MB} MB.", ephemeral=True)
            return

        proof_id = await self.bot_instance.db.create_proof(interaction.user.id)
        webhook_sent = await send_webhook_proof(self.bot_instance, proof_id, interaction.user, attachment)

        review_channel = self.bot_instance.get_channel(REVIEW_CHANNEL_ID) if REVIEW_CHANNEL_ID else None
        review_message = None
        if review_channel and isinstance(review_channel, discord.TextChannel):
            embed = discord.Embed(title=f"📸 Prova #{proof_id}", color=discord.Color.orange())
            embed.add_field(name="Usuário", value=f"{interaction.user.mention}\n`{interaction.user.id}`", inline=True)
            embed.add_field(name="Recompensa", value=money(REWARD_CENTS), inline=True)
            embed.add_field(name="Status", value="🟡 Pendente", inline=True)
            embed.set_image(url=attachment.url)
            embed.set_footer(text=f"Webhook: {'OK' if webhook_sent else 'não configurado'}")
            review_message = await review_channel.send(embed=embed, view=ReviewView(self.bot_instance, proof_id))
            await self.bot_instance.db.set_proof_messages(proof_id, review_channel.id, review_message.id, webhook_sent)
        else:
            # Mesmo sem canal de revisão, a prova fica pendente e pode ser tratada depois pelo banco.
            await self.bot_instance.db.set_proof_messages(proof_id, 0, 0, webhook_sent)

        try:
            await message.delete()
        except discord.HTTPException:
            pass

        if review_message:
            await interaction.followup.send(f"✅ Prova #{proof_id} enviada para análise. Você receberá a decisão.", ephemeral=True)
        elif webhook_sent:
            await interaction.followup.send(f"✅ Prova #{proof_id} enviada ao webhook dos administradores. Configure REVIEW_CHANNEL_ID para ativar os botões de aprovação.", ephemeral=True)
        else:
            await interaction.followup.send("⚠️ Prova registrada, mas o webhook/canal de revisão não está configurado corretamente. Avise um administrador.", ephemeral=True)

    @button(label="Ver saldo", emoji="💰", style=discord.ButtonStyle.success, custom_id="rewards:balance")
    async def balance(self, interaction: discord.Interaction, button: discord.ui.Button):
        balance = await self.bot_instance.db.get_balance(interaction.user.id)
        approved = await self.bot_instance.db.count_user_proofs(interaction.user.id, "approved")
        pending = await self.bot_instance.db.count_user_proofs(interaction.user.id, "pending")
        embed = discord.Embed(title="💰 Seu saldo", color=discord.Color.green())
        embed.add_field(name="Saldo disponível", value=f"**{money(balance)}**", inline=False)
        embed.add_field(name="Provas aprovadas", value=str(approved), inline=True)
        embed.add_field(name="Provas pendentes", value=str(pending), inline=True)
        if TIKTOK_LITE_LINK:
            embed.add_field(name="Seu link TikTok Lite", value=TIKTOK_LITE_LINK, inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @button(label="Solicitar saque", emoji="💸", style=discord.ButtonStyle.secondary, custom_id="rewards:withdraw")
    async def withdraw(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(WithdrawalAmountModal(self.bot_instance))

    @button(label="Consultar saques", emoji="📋", style=discord.ButtonStyle.secondary, custom_id="rewards:withdrawals")
    async def withdrawals(self, interaction: discord.Interaction, button: discord.ui.Button):
        rows = await self.bot_instance.db.list_withdrawals(interaction.user.id)
        if not rows:
            await interaction.response.send_message("📋 Você ainda não possui saques.", ephemeral=True)
            return
        lines = []
        labels = {"pending": "🟡 Pendente", "completed": "🟢 Concluído", "cancelled": "🔴 Cancelado"}
        for withdrawal_id, amount_cents, status, created_at, *_ in rows:
            dt = datetime.fromisoformat(created_at)
            lines.append(f"`#{withdrawal_id}` {display_dt(created_at)} - {money(amount_cents)} - {labels.get(status, status)}")
        embed = discord.Embed(title="📋 Histórico de saques", description="\n".join(lines), color=discord.Color.blurple())
        await interaction.response.send_message(embed=embed, ephemeral=True)


class RewardsPanelView(PanelView):
    pass


class WithdrawalAmountModal(Modal, title="💸 Solicitar saque"):
    amount = TextInput(label="Valor do saque", placeholder="Ex.: 10,00", required=True, max_length=20)

    def __init__(self, bot_instance: Bot):
        super().__init__()
        self.bot_instance = bot_instance

    async def on_submit(self, interaction: discord.Interaction):
        cents = parse_money(self.amount.value)
        if cents is None:
            await interaction.response.send_message("❌ Informe um valor válido. Exemplo: `25,00`.", ephemeral=True)
            return
        if cents < MIN_WITHDRAWAL_CENTS:
            await interaction.response.send_message("❌ O saque mínimo é de R$ 10,00.", ephemeral=True)
            return
        balance = await self.bot_instance.db.get_balance(interaction.user.id)
        if cents > balance:
            await interaction.response.send_message(f"❌ Saldo insuficiente. Seu saldo é {money(balance)}.", ephemeral=True)
            return
        # Discord não permite abrir outro Modal diretamente a partir de um ModalSubmit.
        # Primeiro mostramos um botão; o clique nesse botão abre o formulário PIX.
        await interaction.response.send_message(
            f"✅ Valor de saque: **{money(cents)}**\n"
            "Clique abaixo para informar seus dados PIX.",
            ephemeral=True,
            view=ContinuePixView(self.bot_instance, cents),
        )


class ContinuePixView(View):
    def __init__(self, bot_instance: Bot, amount_cents: int):
        super().__init__(timeout=300)
        self.bot_instance = bot_instance
        self.amount_cents = amount_cents

    @discord.ui.button(label="Continuar com PIX", emoji="💳", style=discord.ButtonStyle.primary)
    async def continue_pix(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(PixDataModal(self.bot_instance, self.amount_cents))


class PixDataModal(Modal, title="💳 Dados PIX"):
    pix_type = TextInput(label="Tipo de chave PIX", placeholder="CPF, CNPJ, E-mail, Telefone ou Aleatória", required=True, max_length=30)
    recipient_name = TextInput(label="Nome do recebedor", placeholder="Nome completo", required=True, max_length=150)
    pix_key = TextInput(label="Chave PIX", placeholder="Informe a chave", required=True, max_length=200)

    def __init__(self, bot_instance: Bot, amount_cents: int):
        super().__init__()
        self.bot_instance = bot_instance
        self.amount_cents = amount_cents

    async def on_submit(self, interaction: discord.Interaction):
        ok, message, withdrawal_id = await self.bot_instance.db.create_withdrawal(
            interaction.user.id,
            self.amount_cents,
            self.pix_type.value.strip(),
            self.recipient_name.value.strip(),
            self.pix_key.value.strip(),
        )
        if not ok:
            await interaction.response.send_message(f"❌ {message}", ephemeral=True)
            return

        await interaction.response.send_message(
            f"✅ Saque **#{withdrawal_id}** criado.\n💰 Valor: **{money(self.amount_cents)}**\n🟡 Status: **Pendente**",
            ephemeral=True,
        )
        await notify_new_withdrawal(self.bot_instance, withdrawal_id)


class RejectProofModal(Modal, title="❌ Reprovar prova"):
    reason = TextInput(label="Motivo", style=discord.TextStyle.paragraph, placeholder="Explique resumidamente o motivo da reprovação.", required=True, max_length=500)

    def __init__(self, bot_instance: Bot, proof_id: int):
        super().__init__()
        self.bot_instance = bot_instance
        self.proof_id = proof_id

    async def on_submit(self, interaction: discord.Interaction):
        if not admin_check(interaction):
            await interaction.response.send_message("❌ Você não tem permissão.", ephemeral=True)
            return
        ok, message, user_id = await self.bot_instance.db.reject_proof(self.proof_id, interaction.user.id, self.reason.value.strip())
        if not ok:
            await interaction.response.send_message(f"❌ {message}", ephemeral=True)
            return
        await interaction.response.send_message(f"✅ {message}", ephemeral=True)
        await update_review_message(self.bot_instance, self.proof_id, "rejected", interaction.user, self.reason.value.strip())
        await notify_user(self.bot_instance, user_id, f"❌ Sua prova **#{self.proof_id}** foi reprovada.\nMotivo: {self.reason.value.strip()}")


class ReviewView(View):
    def __init__(self, bot_instance: Bot, proof_id: int):
        super().__init__(timeout=None)
        self.bot_instance = bot_instance
        self.proof_id = proof_id

        approve = discord.ui.Button(label="Aprovar", emoji="✅", style=discord.ButtonStyle.success, custom_id=f"proof:approve:{proof_id}")
        reject = discord.ui.Button(label="Reprovar", emoji="❌", style=discord.ButtonStyle.danger, custom_id=f"proof:reject:{proof_id}")
        approve.callback = self.approve_callback
        reject.callback = self.reject_callback
        self.add_item(approve)
        self.add_item(reject)

    async def approve_callback(self, interaction: discord.Interaction):
        if not admin_check(interaction):
            await interaction.response.send_message("❌ Você não tem permissão para analisar provas.", ephemeral=True)
            return
        ok, message, user_id = await self.bot_instance.db.approve_proof(self.proof_id, interaction.user.id)
        if not ok:
            await interaction.response.send_message(f"❌ {message}", ephemeral=True)
            return
        await interaction.response.send_message("✅ Prova aprovada.", ephemeral=True)
        await update_review_message(self.bot_instance, self.proof_id, "approved", interaction.user)
        await notify_user(self.bot_instance, user_id, f"✅ Sua prova **#{self.proof_id}** foi aprovada! Você recebeu **{money(REWARD_CENTS)}** de saldo.")

    async def reject_callback(self, interaction: discord.Interaction):
        if not admin_check(interaction):
            await interaction.response.send_message("❌ Você não tem permissão para analisar provas.", ephemeral=True)
            return
        await interaction.response.send_modal(RejectProofModal(self.bot_instance, self.proof_id))


async def update_review_message(bot_instance: Bot, proof_id: int, status: str, reviewer: discord.Member, reason: str = ""):
    row = await bot_instance.db.get_proof(proof_id)
    if not row:
        return
    # columns: id,user_id,status,created_at,reviewed_at,reviewer_id,rejection_reason,review_message_id,review_channel_id,webhook_sent
    message_id = row[7]
    channel_id = row[8]
    if not message_id or not channel_id:
        return
    channel = bot_instance.get_channel(channel_id)
    if not channel or not isinstance(channel, discord.TextChannel):
        return
    try:
        message = await channel.fetch_message(message_id)
        color = discord.Color.green() if status == "approved" else discord.Color.red()
        status_text = "🟢 Aprovada" if status == "approved" else "🔴 Reprovada"
        embed = message.embeds[0] if message.embeds else discord.Embed(title=f"📸 Prova #{proof_id}")
        embed.color = color
        # update/add fields by rebuilding for predictable layout
        user_field = next((f for f in embed.fields if f.name == "Usuário"), None)
        user_value = user_field.value if user_field else "Desconhecido"
        embed.clear_fields()
        embed.add_field(name="Usuário", value=user_value, inline=True)
        embed.add_field(name="Recompensa", value=money(REWARD_CENTS), inline=True)
        embed.add_field(name="Status", value=f"{status_text} por {reviewer.mention}", inline=True)
        if reason:
            embed.add_field(name="Motivo", value=reason[:1000], inline=False)
        await message.edit(embed=embed, view=None)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        pass


async def notify_user(bot_instance: Bot, user_id: Optional[int], content: str):
    if not user_id:
        return
    try:
        user = bot_instance.get_user(user_id) or await bot_instance.fetch_user(user_id)
        await user.send(content)
    except (discord.Forbidden, discord.NotFound, discord.HTTPException):
        pass


async def notify_new_withdrawal(bot_instance: Bot, withdrawal_id: int):
    if not REVIEW_CHANNEL_ID:
        return
    channel = bot_instance.get_channel(REVIEW_CHANNEL_ID)
    if not channel or not isinstance(channel, discord.TextChannel):
        return
    rows = [r for r in await bot_instance.db.pending_withdrawals() if r[0] == withdrawal_id]
    if not rows:
        return
    _, user_id, amount_cents, pix_type, recipient_name, pix_key, status, created_at = rows[0]
    embed = discord.Embed(title=f"💸 Novo saque #{withdrawal_id}", color=discord.Color.gold())
    embed.add_field(name="Usuário", value=f"<@{user_id}>\n`{user_id}`", inline=False)
    embed.add_field(name="Valor", value=money(amount_cents), inline=True)
    embed.add_field(name="Status", value="🟡 Pendente", inline=True)
    embed.add_field(name="Tipo PIX", value=pix_type, inline=True)
    embed.add_field(name="Recebedor", value=recipient_name, inline=False)
    embed.add_field(name="Chave PIX", value=f"`{pix_key}`", inline=False)
    embed.set_footer(text=f"Solicitado em {display_dt(created_at)}")
    await channel.send(embed=embed, view=WithdrawalReviewView(bot_instance, withdrawal_id))


class WithdrawalReviewView(View):
    def __init__(self, bot_instance: Bot, withdrawal_id: int):
        super().__init__(timeout=None)
        self.bot_instance = bot_instance
        self.withdrawal_id = withdrawal_id
        complete = discord.ui.Button(label="Marcar concluído", emoji="✅", style=discord.ButtonStyle.success, custom_id=f"withdraw:complete:{withdrawal_id}")
        cancel = discord.ui.Button(label="Cancelar e devolver saldo", emoji="↩️", style=discord.ButtonStyle.danger, custom_id=f"withdraw:cancel:{withdrawal_id}")
        complete.callback = self.complete_callback
        cancel.callback = self.cancel_callback
        self.add_item(complete)
        self.add_item(cancel)

    async def complete_callback(self, interaction: discord.Interaction):
        if not admin_check(interaction):
            await interaction.response.send_message("❌ Sem permissão.", ephemeral=True)
            return
        # Guardamos o valor ANTES de mudar o status para "completed".
        # Depois da atualização, o saque deixa de aparecer em pending_withdrawals(),
        # que fazia a mensagem anterior mostrar R$ 0,00.
        rows = [r for r in await self.bot_instance.db.pending_withdrawals() if r[0] == self.withdrawal_id]
        if not rows:
            await interaction.response.send_message("❌ Este saque não está mais pendente.", ephemeral=True)
            return

        _, pending_user_id, amount_cents, *_ = rows[0]

        ok, message, user_id = await self.bot_instance.db.update_withdrawal_status(
            self.withdrawal_id, "completed", interaction.user.id
        )
        if not ok:
            await interaction.response.send_message(f"❌ {message}", ephemeral=True)
            return

        await interaction.response.send_message("✅ Saque marcado como concluído.", ephemeral=True)
        await self.disable_buttons(interaction.message, "🟢 Concluído")

        await notify_user(
            self.bot_instance,
            user_id or pending_user_id,
            f"✅ Seu saque **#{self.withdrawal_id}** no valor de **{money(amount_cents)}** foi marcado como concluído."
        )

    async def cancel_callback(self, interaction: discord.Interaction):
        if not admin_check(interaction):
            await interaction.response.send_message("❌ Sem permissão.", ephemeral=True)
            return
        # Fetch before updating for the notification amount.
        rows = [r for r in await self.bot_instance.db.pending_withdrawals() if r[0] == self.withdrawal_id]
        amount = rows[0][2] if rows else 0
        ok, message, user_id = await self.bot_instance.db.update_withdrawal_status(self.withdrawal_id, "cancelled", interaction.user.id)
        if not ok:
            await interaction.response.send_message(f"❌ {message}", ephemeral=True)
            return
        await interaction.response.send_message("✅ Saque cancelado e saldo devolvido.", ephemeral=True)
        await self.disable_buttons(interaction.message, "🔴 Cancelado")
        await notify_user(self.bot_instance, user_id, f"🔴 Seu saque **#{self.withdrawal_id}** foi cancelado. **{money(amount)}** voltou para o seu saldo.")

    async def disable_buttons(self, message: discord.Message, status_text: str):
        try:
            embed = message.embeds[0] if message.embeds else discord.Embed(title=f"💸 Saque #{self.withdrawal_id}")
            # preserve existing fields and change Status if present
            fields = list(embed.fields)
            embed.clear_fields()
            for field in fields:
                if field.name == "Status":
                    embed.add_field(name="Status", value=status_text, inline=field.inline)
                else:
                    embed.add_field(name=field.name, value=field.value, inline=field.inline)
            await message.edit(embed=embed, view=None)
        except (discord.HTTPException, discord.Forbidden, discord.NotFound):
            pass


@bot.tree.command(name="painel", description="Envia o painel de recompensas TikTok Lite")
async def painel(interaction: discord.Interaction):
    if not admin_check(interaction):
        await interaction.response.send_message("❌ Apenas administradores podem usar /painel.", ephemeral=True)
        return
    embed = discord.Embed(
        title="🎁 Programa de Recompensas TikTok Lite",
        description=(
            "Compartilhe seu convite e envie a comprovação para receber sua recompensa.\n\n"
            f"💰 **Recompensa por prova aprovada:** {money(REWARD_CENTS)}\n"
            f"💸 **Saque mínimo:** {money(MIN_WITHDRAWAL_CENTS)}"
        ),
        color=discord.Color.blurple(),
    )
    if TIKTOK_LITE_LINK:
        embed.add_field(name="🔗 Link TikTok Lite", value=TIKTOK_LITE_LINK, inline=False)
    embed.set_footer(text="Provas são analisadas pelos administradores.")
    await interaction.response.send_message(embed=embed, view=RewardsPanelView(bot))


@bot.tree.command(name="saques_pendentes", description="Lista saques pendentes (administrador)")
async def saques_pendentes(interaction: discord.Interaction):
    if not admin_check(interaction):
        await interaction.response.send_message("❌ Sem permissão.", ephemeral=True)
        return
    rows = await bot.db.pending_withdrawals()
    if not rows:
        await interaction.response.send_message("✅ Não há saques pendentes.", ephemeral=True)
        return
    lines = []
    for withdrawal_id, user_id, amount_cents, pix_type, recipient_name, pix_key, status, created_at in rows:
        dt = datetime.fromisoformat(created_at)
        lines.append(f"**#{withdrawal_id}** • <@{user_id}> • {money(amount_cents)} • {pix_type} • {dt.strftime('%d/%m/%Y %H:%M')}")
    await interaction.response.send_message("💸 **Saques pendentes**\n\n" + "\n".join(lines), ephemeral=True)


@bot.tree.command(name="prova_pendente", description="Consulta uma prova pendente pelo ID (administrador)")
@app_commands.describe(proof_id="ID da prova")
async def prova_pendente(interaction: discord.Interaction, proof_id: int):
    if not admin_check(interaction):
        await interaction.response.send_message("❌ Sem permissão.", ephemeral=True)
        return
    row = await bot.db.get_proof(proof_id)
    if not row:
        await interaction.response.send_message("❌ Prova não encontrada.", ephemeral=True)
        return
    await interaction.response.send_message(
        f"📸 Prova #{proof_id}\nUsuário: <@{row[1]}>\nStatus: **{row[2]}**\nCriada em: {display_dt(row[3])}",
        ephemeral=True,
    )


@bot.tree.command(name="ajustar_saldo", description="Adiciona ou remove saldo de um usuário")
@app_commands.describe(usuario="Usuário", valor="Ex.: 5,00 ou -5,00", motivo="Motivo do ajuste")
async def ajustar_saldo(interaction: discord.Interaction, usuario: discord.Member, valor: str, motivo: str):
    if not admin_check(interaction):
        await interaction.response.send_message("❌ Sem permissão.", ephemeral=True)
        return
    cents = parse_money(valor)
    if cents is None:
        # support explicit negative form
        raw = valor.strip().replace("r$", "").replace(" ", "")
        try:
            if "," in raw:
                raw = raw.replace(".", "").replace(",", ".")
            cents = int(Decimal(raw).quantize(Decimal("0.01")) * 100)
        except Exception:
            cents = None
    if cents is None:
        await interaction.response.send_message("❌ Valor inválido.", ephemeral=True)
        return
    ok, message = await bot.db.admin_adjust_balance(usuario.id, cents, interaction.user.id, motivo)
    if not ok:
        await interaction.response.send_message(f"❌ {message}", ephemeral=True)
        return
    await interaction.response.send_message(f"✅ {message} Novo saldo: **{money(await bot.db.get_balance(usuario.id))}**", ephemeral=True)


@bot.tree.command(name="aprovar_prova", description="Aprova uma prova pelo ID (administrador)")
@app_commands.describe(proof_id="ID da prova")
async def aprovar_prova(interaction: discord.Interaction, proof_id: int):
    if not admin_check(interaction):
        await interaction.response.send_message("❌ Sem permissão.", ephemeral=True)
        return
    ok, message, user_id = await bot.db.approve_proof(proof_id, interaction.user.id)
    if not ok:
        await interaction.response.send_message(f"❌ {message}", ephemeral=True)
        return
    await interaction.response.send_message(f"✅ {message}", ephemeral=True)
    await update_review_message(bot, proof_id, "approved", interaction.user)
    await notify_user(bot, user_id, f"✅ Sua prova **#{proof_id}** foi aprovada! Você recebeu **{money(REWARD_CENTS)}** de saldo.")


@bot.tree.command(name="reprovar_prova", description="Reprova uma prova pelo ID (administrador)")
@app_commands.describe(proof_id="ID da prova", motivo="Motivo da reprovação")
async def reprovar_prova(interaction: discord.Interaction, proof_id: int, motivo: str):
    if not admin_check(interaction):
        await interaction.response.send_message("❌ Sem permissão.", ephemeral=True)
        return
    ok, message, user_id = await bot.db.reject_proof(proof_id, interaction.user.id, motivo)
    if not ok:
        await interaction.response.send_message(f"❌ {message}", ephemeral=True)
        return
    await interaction.response.send_message(f"✅ {message}", ephemeral=True)
    await update_review_message(bot, proof_id, "rejected", interaction.user, motivo)
    await notify_user(bot, user_id, f"❌ Sua prova **#{proof_id}** foi reprovada.\nMotivo: {motivo[:500]}")


@bot.tree.command(name="estatisticas", description="Mostra estatísticas administrativas")
async def estatisticas(interaction: discord.Interaction):
    if not admin_check(interaction):
        await interaction.response.send_message("❌ Sem permissão.", ephemeral=True)
        return
    s = await bot.db.stats()
    embed = discord.Embed(title="📊 Estatísticas", color=discord.Color.blurple())
    embed.add_field(name="Usuários", value=str(s["users"]), inline=True)
    embed.add_field(name="Provas pendentes", value=str(s["pending_proofs"]), inline=True)
    embed.add_field(name="Provas aprovadas", value=str(s["approved_proofs"]), inline=True)
    embed.add_field(name="Saques pendentes", value=str(s["pending_withdrawals"]), inline=True)
    embed.add_field(name="Total concluído", value=money(s["completed_withdrawals"]), inline=True)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="meulink", description="Mostra o link do TikTok Lite configurado")
async def meulink(interaction: discord.Interaction):
    if not TIKTOK_LITE_LINK:
        await interaction.response.send_message("⚠️ Nenhum link foi configurado em TIKTOK_LITE_LINK.", ephemeral=True)
        return
    await interaction.response.send_message(f"🔗 **Seu link TikTok Lite:**\n{TIKTOK_LITE_LINK}", ephemeral=True)


@bot.event
async def on_command_error(ctx: commands.Context, error: Exception):
    if isinstance(error, commands.CommandNotFound):
        return
    print(f"Erro de comando: {error}")


async def main():
    if not TOKEN:
        raise SystemExit("DISCORD_TOKEN não configurado. Copie .env.example para .env e preencha o token.")
    await bot.start(TOKEN)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
