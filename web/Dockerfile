# Copyright (c) 2026 Baynham Makusha. All rights reserved.
# Unauthorized copying, distribution, or use is prohibited.

# ---- Build stage ----------------------------------------------------------
FROM node:20-alpine AS builder
WORKDIR /app

COPY package*.json ./
RUN npm ci

COPY . .

# NEXT_PUBLIC_* values are inlined into the browser bundle at build time, so
# they must be present before `next build`. The Firebase web config is public
# by design (it ships in every client). NEXT_PUBLIC_API_BASE points at the
# deployed hermes-api Cloud Run service and can be overridden with --build-arg.
ARG NEXT_PUBLIC_FIREBASE_API_KEY=AIzaSyBSj-y9gCo71VzG7CTQ791VXjv-5QcVdiw
ARG NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN=hermes-agent-btm-001.firebaseapp.com
ARG NEXT_PUBLIC_FIREBASE_PROJECT_ID=hermes-agent-btm-001
ARG NEXT_PUBLIC_FIREBASE_APP_ID=1:812714701339:web:d258dd8cc3084a76f23ebe
ARG NEXT_PUBLIC_API_BASE=https://hermes-api-812714701339.us-central1.run.app
# Invite codes that unlock email/password account creation on /login (soft,
# client-side gate — see .env.example). Empty default = signup disabled; set
# real codes here, or pass --build-arg, to enable it in the deployed image.
ARG NEXT_PUBLIC_INVITE_CODES=HERMES-PR0D

ENV NEXT_PUBLIC_FIREBASE_API_KEY=$NEXT_PUBLIC_FIREBASE_API_KEY \
    NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN=$NEXT_PUBLIC_FIREBASE_AUTH_DOMAIN \
    NEXT_PUBLIC_FIREBASE_PROJECT_ID=$NEXT_PUBLIC_FIREBASE_PROJECT_ID \
    NEXT_PUBLIC_FIREBASE_APP_ID=$NEXT_PUBLIC_FIREBASE_APP_ID \
    NEXT_PUBLIC_API_BASE=$NEXT_PUBLIC_API_BASE \
    NEXT_PUBLIC_INVITE_CODES=$NEXT_PUBLIC_INVITE_CODES

RUN npm run build

# ---- Runtime stage --------------------------------------------------------
FROM node:20-alpine
WORKDIR /app
ENV NODE_ENV=production

# Standalone output bundles a minimal node_modules + server.js. Static assets
# and the public/ dir are not included automatically, so copy them in.
COPY --from=builder /app/public ./public
COPY --from=builder /app/.next/standalone ./
COPY --from=builder /app/.next/static ./.next/static

ENV PORT=3000
EXPOSE 3000
CMD ["node", "server.js"]
