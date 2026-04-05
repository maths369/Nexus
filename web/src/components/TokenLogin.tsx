import { FormEvent, useEffect, useState } from "react";

interface TokenLoginProps {
  title?: string;
  description?: string;
  busy?: boolean;
  error?: string;
  initialToken?: string;
  onSubmit: (token: string) => Promise<void> | void;
}

export default function TokenLogin({
  title = "登录 Nexus Workspace",
  description = "输入 Bearer Token 以访问移动工作区。",
  busy = false,
  error = "",
  initialToken = "",
  onSubmit,
}: TokenLoginProps) {
  const [token, setToken] = useState(initialToken);

  useEffect(() => {
    setToken(initialToken);
  }, [initialToken]);

  const handleSubmit = async (event: FormEvent) => {
    event.preventDefault();
    const value = token.trim();
    if (!value || busy) return;
    await onSubmit(value);
  };

  return (
    <div className="token-login-shell">
      <div className="token-login-card">
        <div className="token-login-head">
          <span className="token-login-badge">Mobile Workspace</span>
          <h1>{title}</h1>
          <p>{description}</p>
        </div>
        <form className="token-login-form" onSubmit={handleSubmit}>
          <label htmlFor="nexus-token">访问 Token</label>
          <input
            id="nexus-token"
            type="password"
            autoComplete="current-password"
            spellCheck={false}
            value={token}
            onChange={(event) => setToken(event.target.value)}
            placeholder="Bearer Token"
          />
          {error && <p className="token-login-error">{error}</p>}
          <button type="submit" disabled={busy || !token.trim()}>
            {busy ? "验证中..." : "进入工作区"}
          </button>
        </form>
      </div>
    </div>
  );
}
