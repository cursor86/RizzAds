// Decides whether the signed-in user may act on a given client's social accounts.
//
// This is deny-by-default on purpose. Connecting an account grants publishing rights, so the proxy refuses every
// request until this function is wired to your auth session and your client-membership table. Implement it
// before deploying: read the session from the request, then confirm the user is a member of clientId.

import type { NextRequest } from 'next/server';

export async function authorizeClientAccess(_request: NextRequest, _clientId: number): Promise<boolean> {
  return false;
}
