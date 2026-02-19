#!/bin/bash
# CypherGrokTrade - Deploy to Koyeb (FREE tier)
# Usage: ./deploy.sh
#
# Pre-requisitos:
# 1. Criar conta gratis em https://app.koyeb.com (login com GitHub)
# 2. Instalar Koyeb CLI: curl -fsSL https://raw.githubusercontent.com/koyeb/koyeb-cli/master/install.sh | sh
# 3. Login: koyeb login
# 4. Rodar este script

set -e

APP_NAME="cyphergroktrade"

echo "=== CypherGrokTrade - Deploy Koyeb ==="
echo ""

# Check if koyeb CLI is installed
if ! command -v koyeb &> /dev/null; then
    echo "Koyeb CLI nao encontrado. Instalando..."
    curl -fsSL https://raw.githubusercontent.com/koyeb/koyeb-cli/master/install.sh | sh
    echo "Agora faca login: koyeb login"
    exit 1
fi

echo "Criando app no Koyeb (free tier)..."
echo ""
echo "IMPORTANTE: Configure as env vars no dashboard do Koyeb:"
echo "  HL_PRIVATE_KEY       = sua chave privada"
echo "  HL_WALLET_ADDRESS    = seu endereco wallet"
echo "  WITHDRAW_WALLET      = wallet de withdrawal"
echo "  TELEGRAM_BOT_TOKEN   = token do bot telegram"
echo "  TELEGRAM_CHAT_ID     = chat ID telegram"
echo "  GROK_API_KEY         = chave API do Grok"
echo ""

# Deploy from GitHub (Koyeb builds the Docker image)
koyeb app create "$APP_NAME" 2>/dev/null || true

koyeb service create "$APP_NAME/bot" \
    --git "github.com/0xjc65eth/cyphergroktrade" \
    --git-branch "master" \
    --git-docker-dockerfile "Dockerfile" \
    --instance-type "free" \
    --regions "was" \
    --env "HL_PRIVATE_KEY=CHANGE_ME" \
    --env "HL_WALLET_ADDRESS=CHANGE_ME" \
    --env "WITHDRAW_WALLET=CHANGE_ME" \
    --env "TELEGRAM_BOT_TOKEN=CHANGE_ME" \
    --env "TELEGRAM_CHAT_ID=CHANGE_ME" \
    --env "GROK_API_KEY=CHANGE_ME"

echo ""
echo "=== Deploy iniciado! ==="
echo "1. Va em https://app.koyeb.com"
echo "2. Clique no servico '$APP_NAME'"
echo "3. Va em Settings > Environment Variables"
echo "4. Troque os 'CHANGE_ME' pelos valores reais"
echo "5. Clique Redeploy"
echo ""
echo "O bot vai rodar 24/7 gratis!"
