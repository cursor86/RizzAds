import React, { useEffect, useState } from 'react';

// Public app id only. The app secret stays on the server.
const META_APP_ID = process.env.NEXT_PUBLIC_META_APP_ID;
// Must match ORGANIC_API_VERSION on the AdEngine server.
const GRAPH_VERSION = 'v19.0';
const LOGIN_SCOPES = [
  'pages_show_list',
  'pages_read_engagement',
  'pages_manage_posts',
  'instagram_basic',
  'instagram_content_publish',
];
// Browser calls go through the server proxy in web/app/api/adengine, which adds the API key.
const API = '/api/adengine';

export default function ClientOnboarding({ clientId }) {
  const [sdkReady, setSdkReady] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [pages, setPages] = useState([]);
  const [connected, setConnected] = useState(null);

  useEffect(() => {
    window.fbAsyncInit = () => {
      window.FB.init({ appId: META_APP_ID, cookie: true, xfbml: false, version: GRAPH_VERSION });
      setSdkReady(true);
    };
    if (document.getElementById('facebook-jssdk')) {
      if (window.FB) setSdkReady(true);
      return;
    }
    const js = document.createElement('script');
    js.id = 'facebook-jssdk';
    js.src = 'https://connect.facebook.net/en_US/sdk.js';
    js.async = true;
    document.body.appendChild(js);
  }, []);

  const post = async (path, payload) => {
    const res = await fetch(`${API}/brands/${clientId}/organic/${path}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const data = await res.json().catch(() => ({}));
    return { ok: res.ok, data };
  };

  const selectPage = async (pageId) => {
    setBusy(true);
    setError(null);
    try {
      const { ok, data } = await post('page', { page_id: pageId });
      if (!ok) {
        setError(data.detail || 'Could not select that Page.');
        return;
      }
      setConnected(data.page);
    } catch {
      setError('Network error while selecting the Page. Try again.');
    } finally {
      setBusy(false);
    }
  };

  const connectAccount = async (shortLivedToken) => {
    try {
      const { ok, data } = await post('connect', { short_lived_token: shortLivedToken });
      if (!ok) {
        setError(typeof data.detail === 'string' ? data.detail : 'Could not connect the account. Try again.');
        return;
      }
      setPages(data.pages);
      if (data.pages.length === 1) {
        await selectPage(data.pages[0].page_id);
      }
    } catch {
      setError('Network error while connecting. Check your connection and try again.');
    } finally {
      setBusy(false);
    }
  };

  const handleMetaLogin = () => {
    setError(null);
    if (!sdkReady || !window.FB) {
      setError('Facebook login is still loading. Try again in a moment.');
      return;
    }
    setBusy(true);
    window.FB.login(
      (response) => {
        const token = response.authResponse?.accessToken;
        if (!token) {
          setError('Login was cancelled or not every permission was approved.');
          setBusy(false);
          return;
        }
        connectAccount(token);
      },
      { scope: LOGIN_SCOPES.join(','), return_scopes: true },
    );
  };

  return (
    <div className="max-w-xl mx-auto my-12 p-8 bg-white rounded-xl shadow-md border border-gray-100">
      <h2 className="text-2xl font-bold text-gray-900">Connect your social accounts</h2>
      <p className="text-gray-500 mt-2 text-sm leading-relaxed">
        This lets us publish Reels to your Facebook Page and linked Instagram account. We only request the
        permissions needed for publishing. You can disconnect at any time.
      </p>

      {error && (
        <p role="alert" className="mt-4 text-sm text-red-700 bg-red-50 border border-red-200 rounded-lg p-3">
          {error}
        </p>
      )}

      {!connected && pages.length === 0 && (
        <button
          onClick={handleMetaLogin}
          disabled={busy || !sdkReady}
          className="mt-6 w-full py-3 px-4 rounded-lg font-medium text-white bg-blue-600 hover:bg-blue-700 disabled:bg-blue-400 disabled:cursor-not-allowed"
        >
          {busy ? 'Connecting…' : 'Connect Facebook & Instagram'}
        </button>
      )}

      {!connected && pages.length > 1 && (
        <div className="mt-6 space-y-3">
          <p className="text-sm font-medium text-gray-800">Choose the Page to publish to:</p>
          {pages.map((page) => (
            <button
              key={page.page_id}
              onClick={() => selectPage(page.page_id)}
              disabled={busy}
              className="w-full text-left p-4 rounded-lg border border-gray-200 hover:border-blue-400"
            >
              <span className="font-semibold text-gray-800 text-sm">{page.name}</span>
              <span className="block text-xs text-gray-500">
                {page.instagram_username ? `Instagram: @${page.instagram_username}` : 'No Instagram account linked'}
              </span>
            </button>
          ))}
        </div>
      )}

      {connected && (
        <div className="mt-6 p-4 bg-green-50 rounded-lg border border-green-200">
          <p className="text-sm font-semibold text-green-800">Connected to {connected.name}</p>
          <p className="text-xs text-green-700 mt-1">
            {connected.instagram_username
              ? `Instagram: @${connected.instagram_username}`
              : 'No Instagram account is linked to this Page, so only Facebook publishing is enabled.'}
          </p>
        </div>
      )}
    </div>
  );
}
