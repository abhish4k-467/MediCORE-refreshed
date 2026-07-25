"use client";

import { useEffect, useState, Suspense } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { Loader2, AlertTriangle, ShieldCheck, Lock, Eye, EyeOff, CheckCircle } from "lucide-react";
import Loader from "@/components/Loader";
import { getApiBaseUrl } from "@/lib/api";

function ResetPasswordContent() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const token = searchParams.get("token");

  const [loading, setLoading] = useState(true);
  const [verifying, setVerifying] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [password, setPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const [showConfirmPassword, setShowConfirmPassword] = useState(false);
  const [success, setSuccess] = useState(false);
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    if (!token) {
      setError("Password reset token is missing. Please check your email link.");
      setVerifying(false);
      setLoading(false);
      return;
    }

    const apiUrl = getApiBaseUrl();

    // Call backend endpoint to verify reset token
    fetch(`${apiUrl}/api/admin/reset-password/verify?token=${token}`)
      .then(async (res) => {
        if (!res.ok) {
          const detail = await res.json().catch(() => ({}));
          throw new Error(detail.detail || "Invalid or expired password reset link.");
        }
        return res.json();
      })
      .then(() => {
        setVerifying(false);
        setLoading(false);
      })
      .catch((err) => {
        setError(err.message);
        setVerifying(false);
        setLoading(false);
      });
  }, [token]);

  async function handleResetSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);

    if (password.length < 6) {
      setError("Password must be at least 6 characters.");
      return;
    }

    if (password !== confirmPassword) {
      setError("Passwords do not match.");
      return;
    }

    setSubmitting(true);
    const apiUrl = getApiBaseUrl();

    try {
      const response = await fetch(`${apiUrl}/api/admin/reset-password/complete`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          token: token,
          password: password,
        }),
      });

      if (!response.ok) {
        const detail = await response.json().catch(() => ({}));
        throw new Error(detail.detail || "Failed to update password.");
      }

      setSuccess(true);
    } catch (err: any) {
      setError(err.message);
    } finally {
      setSubmitting(false);
    }
  }

  if (success) {
    return (
      <main className="auth-page">
        <div className="auth-card-wrapper">
          <div className="auth-card-glow"></div>
          <div className="auth-card" style={{ textAlign: "center", padding: "40px" }}>
            <div style={{
              display: "inline-flex",
              alignItems: "center",
              justifyContent: "center",
              width: "56px",
              height: "56px",
              background: "rgba(15, 122, 95, 0.08)",
              color: "#0f7a5f",
              borderRadius: "50%",
              marginBottom: "16px",
            }}>
              <CheckCircle size={28} />
            </div>
            <h2 style={{ fontSize: "20px", fontWeight: 700, color: "#17211c", margin: "0 0 8px 0" }}>Password Updated</h2>
            <p style={{ fontSize: "14px", color: "#66736d", lineHeight: 1.6, margin: "0 0 24px 0" }}>
              Your password has been successfully updated. You can now use your new password to sign into MediCORE.
            </p>
            <button
              onClick={() => router.push("/login")}
              className="auth-submit-btn"
              style={{ width: "100%" }}
            >
              Sign In
            </button>
          </div>
        </div>
      </main>
    );
  }

  return (
    <main className="auth-page">
      <div className="auth-card-wrapper">
        <div className="auth-card-glow"></div>
        <form className="auth-card" onSubmit={handleResetSubmit}>
          <div className="auth-brand" style={{ textAlign: "center", marginBottom: "24px" }}>
            <div className="brand-logo" style={{ background: "transparent", width: "64px", height: "64px", padding: 0, display: "inline-flex", justifyContent: "center", alignItems: "center", marginBottom: "12px" }}>
              <img src="/Tarkshy.png" alt="Tarkshy Logo" style={{ width: "100%", height: "100%", objectFit: "contain" }} />
            </div>
            <h1>MediCORE</h1>
          </div>

          <div className="auth-header-text" style={{ textAlign: "center", marginBottom: "20px" }}>
            <h2>Reset Password</h2>
            <p>Please enter your new password below.</p>
          </div>

          {error && (
            <div className="auth-error-box" style={{ marginBottom: "20px" }}>
              <ShieldCheck className="error-icon" />
              <span>{error}</span>
            </div>
          )}

          {verifying ? (
            <div style={{ display: "flex", justifyContent: "center", padding: "20px" }}>
              <Loader2 className="animate-spin" style={{ color: "#0f7a5f" }} />
            </div>
          ) : (
            <>
              <div className="input-group" style={{ marginBottom: "20px" }}>
                <label htmlFor="password">
                  <span>New Password</span>
                  <div className="input-with-icon">
                    <Lock className="field-icon" />
                    <input
                      id="password"
                      type={showPassword ? "text" : "password"}
                      value={password}
                      onChange={(e) => setPassword(e.target.value)}
                      placeholder="••••••••"
                      required
                      autoComplete="new-password"
                    />
                    <button
                      type="button"
                      onClick={() => setShowPassword(!showPassword)}
                      className="password-toggle-btn"
                    >
                      {showPassword ? <EyeOff size={18} /> : <Eye size={18} />}
                    </button>
                  </div>
                </label>
              </div>

              <div className="input-group" style={{ marginBottom: "24px" }}>
                <label htmlFor="confirmPassword">
                  <span>Confirm New Password</span>
                  <div className="input-with-icon">
                    <Lock className="field-icon" />
                    <input
                      id="confirmPassword"
                      type={showConfirmPassword ? "text" : "password"}
                      value={confirmPassword}
                      onChange={(e) => setConfirmPassword(e.target.value)}
                      placeholder="••••••••"
                      required
                      autoComplete="new-password"
                    />
                    <button
                      type="button"
                      onClick={() => setShowConfirmPassword(!showConfirmPassword)}
                      className="password-toggle-btn"
                    >
                      {showConfirmPassword ? <EyeOff size={18} /> : <Eye size={18} />}
                    </button>
                  </div>
                </label>
              </div>

              <button type="submit" className="auth-submit-btn" disabled={submitting} style={{ width: "100%" }}>
                {submitting ? (
                  <>
                    <Loader2 className="animate-spin" size={18} style={{ marginRight: "8px" }} />
                    Resetting...
                  </>
                ) : (
                  "Reset Password"
                )}
              </button>
            </>
          )}
        </form>
      </div>
    </main>
  );
}

export default function ResetPasswordPage() {
  return (
    <Suspense fallback={<Loader variant="card" />}>
      <ResetPasswordContent />
    </Suspense>
  );
}
