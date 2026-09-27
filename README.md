# TikTok Lite Rewards Discord Bot — V1

Bot Discord em Python para administrar um programa de recompensas por provas de convites do TikTok Lite.

## Recursos

- `/painel` — envia a embed pública com os 4 botões.
- **📸 Enviar provas** — o usuário clica e envia uma imagem no canal; a prova é salva no SQLite, copiada para um webhook de administradores e enviada para um canal privado de revisão.
- **✅ Aprovar / ❌ Reprovar** — botões disponíveis para administradores no card de revisão.
- Aprovação adiciona **R$ 5,00** ao saldo.
- Reprovação não adiciona saldo e aceita motivo.
- **💰 Ver saldo** — resposta privada (ephemeral).
- **💸 Solicitar saque** — dois modais: valor e dados PIX. O mínimo padrão é **R$ 10,00**.
- O valor do saque fica reservado no momento da solicitação. Se o administrador cancelar, o saldo é devolvido automaticamente.
- **📋 Consultar saques** — histórico privado com data, hora, valor e status.
- `/saques_pendentes` — lista de saques pendentes para administradores.
- `/prova_pendente <id>` — consulta uma prova por ID.
- `/ajustar_saldo <usuario> <valor> <motivo>` — ajuste administrativo de saldo.
- `/estatisticas` — estatísticas básicas.
- `/meulink` — mostra o link do TikTok Lite configurado.
- SQLite com transações para manter histórico financeiro.

## Requisitos

Python **3.10+** recomendado. O pacote atual do `discord.py` usado nesta V1 é `2.7.1`.

## Configuração do Discord

1. Crie uma aplicação em **Discord Developer Portal**.
2. Crie um bot e copie o token.
3. Habilite o intent **Message Content** no Developer Portal. A V1 usa `wait_for("message")` para receber a imagem enviada pelo usuário.
4. Convide o bot para o servidor com os escopos:
   - `bot`
   - `applications.commands`
5. Dê ao bot permissão para:
   - Ver canais
   - Enviar mensagens
   - Gerenciar mensagens (para apagar a prova depois do recebimento, opcional)
   - Anexar arquivos
   - Inserir links
   - Usar comandos de aplicação

## Canal de revisão

Crie um canal privado para os administradores. Coloque o ID dele em `REVIEW_CHANNEL_ID`.

É nesse canal que o bot envia as provas com os botões **Aprovar** e **Reprovar**, além das solicitações de saque.

## Webhook

Crie um webhook no seu canal/área de administradores e coloque a URL em `PROOF_WEBHOOK_URL`.

A V1 usa o webhook para receber uma cópia da prova e seus metadados. Os botões de decisão ficam no canal de revisão do bot, porque os cliques dos componentes precisam ser tratados pela aplicação do bot.

## Instalação no Windows

### Automático

1. Copie `.env.example` para `.env`.
2. Preencha os IDs, o token e o webhook.
3. Execute `run.bat`.

### Manual

```text
py -3 -m pip install -r requirements.txt
py -3 bot.py
```

## Como conseguir IDs

Ative o **Modo Desenvolvedor** no Discord:

`Configurações do Discord → Avançado → Modo Desenvolvedor`

Depois clique com o botão direito no servidor, canal ou cargo e escolha **Copiar ID**.

## Fluxo de teste recomendado

1. Execute o bot.
2. Use `/painel` como administrador.
3. Como um usuário comum, clique em **Enviar provas**.
4. Envie uma imagem no mesmo canal.
5. Confira a cópia no webhook e o card no canal de revisão.
6. Clique em **Aprovar**.
7. Confira o saldo usando **Ver saldo**.
8. Solicite um saque de pelo menos R$ 10,00.
9. O saque aparecerá no canal de revisão.
10. Marque como concluído ou cancele para devolver o saldo.

## Banco de dados

O arquivo `data/bot.db` é criado automaticamente.

Tabelas:

- `users`
- `proofs`
- `withdrawals`
- `transactions`

## Observações da V1

- Não existe pagamento PIX automático nesta versão. O administrador marca o saque como concluído depois de realizar o pagamento.
- O saldo é armazenado em centavos inteiros, evitando erros comuns de ponto flutuante.
- A aprovação de uma prova é idempotente: uma prova já analisada não pode pagar duas vezes.
- O pedido de saque reserva o saldo imediatamente; cancelar devolve o valor.
- Para produção, recomenda-se colocar o bot em VPS/Windows Server, fazer backup periódico do SQLite e adicionar uma rotina de expiração de provas pendentes.
