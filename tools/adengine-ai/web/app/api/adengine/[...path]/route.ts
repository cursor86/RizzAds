// Server-side proxy to the AdEngine-AI API. The browser calls /api/adengine/..., and this route adds the
// X-API-Key header, so the key never reaches the client bundle.
//
// Only the organic onboarding routes are forwarded. Anything else returns 404.

import { NextRequest, NextResponse } from 'next/server';
import { authorizeClientAccess } from '@/lib/authorize';

const ALLOWED = /^brands\/(\d+)\/organic\/(connect|page|status)$/;

export async function POST(request: NextRequest, context: { params: Promise<{ path: string[] }> }) {
  return forward(request, context, 'POST');
}

export async function GET(request: NextRequest, context: { params: Promise<{ path: string[] }> }) {
  return forward(request, context, 'GET');
}

async function forward(
  request: NextRequest,
  context: { params: Promise<{ path: string[] }> },
  method: 'GET' | 'POST',
) {
  const { path } = await context.params;
  const joined = path.join('/');
  const match = ALLOWED.exec(joined);
  if (!match) {
    return NextResponse.json({ detail: 'Not found' }, { status: 404 });
  }

  const clientId = Number(match[1]);
  const allowed = await authorizeClientAccess(request, clientId);
  if (!allowed) {
    return NextResponse.json({ detail: 'Forbidden' }, { status: 403 });
  }

  const base = process.env.ADENGINE_BASE_URL;
  const apiKey = process.env.ADENGINE_API_KEY;
  if (!base || !apiKey) {
    return NextResponse.json({ detail: 'Server is not configured for AdEngine' }, { status: 500 });
  }

  const body = method === 'POST' ? await request.text() : undefined;
  let upstream: Response;
  try {
    upstream = await fetch(`${base.replace(/\/$/, '')}/${joined}`, {
      method,
      headers: { 'Content-Type': 'application/json', 'X-API-Key': apiKey },
      body,
      cache: 'no-store',
    });
  } catch {
    return NextResponse.json({ detail: 'AdEngine is unreachable. Try again shortly.' }, { status: 502 });
  }

  // Pass the status and JSON through. Upstream error details are kept, but nothing secret is added.
  const text = await upstream.text();
  return new NextResponse(text, {
    status: upstream.status,
    headers: { 'Content-Type': 'application/json' },
  });
}
