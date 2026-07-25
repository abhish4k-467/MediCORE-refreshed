"use client";

import { useEffect, useState, Suspense } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { AlertTriangle, ShieldCheck } from "lucide-react";
import Loader from "@/components/Loader";
import { getApiBaseUrl } from "@/lib/api";

function ActivateContent() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const token = searchParams.get("token");
  
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!token) {
      setError("Activation token is missing. Please check your email invitation link.");
      setLoading(false);
      return;
    }
    
    const apiUrl = getApiBaseUrl();
    
    // Call backend endpoint to verify activation token
    fetch(`${apiUrl}/api/admin/activate/verify?token=${token}`)
      .then(async (res) => {
        if (!res.ok) {
          const detail = await res.json().catch(() => ({}));
          throw new Error(detail.detail || "Invalid or expired activation link.");
        }
        return res.json();
      })
      .then((data) => {
        // Redirect user to the main registration page prefilled with their name and email
        router.push(
          `/register?email=${encodeURIComponent(data.email)}&name=${encodeURIComponent(
            data.name
          )}&token=${token}`
        );
      })
      .catch((err) => {
        setError(err.message);
        setLoading(false);
      });
  }, [token, router]);

  return (
    <main className="auth-page">
      <div className="auth-card-wrapper">
        <div className="auth-card-glow"></div>
        <div className="auth-card" style={{ display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", minHeight: "200px" }}>
          {loading ? (
            <Loader variant="inline" title="Verifying Invitation..." subtitle="Please wait while we validate your token." />
          ) : (
            <div style={{ textAlign: "center", width: "100%" }}>
              <div style={{
                display: "inline-flex",
                alignItems: "center",
                justifyContent: "center",
                width: "56px",
                height: "56px",
                background: "#fdf2f2",
                color: "#9b1c1c",
                borderRadius: "50%",
                marginBottom: "16px",
              }}>
                <AlertTriangle size={28} />
              </div>
              <h2 style={{ fontSize: "20px", fontWeight: 700, color: "#17211c", margin: "0 0 8px 0" }}>Activation Failed</h2>
              <div className="auth-error-box" style={{ margin: "16px 0", display: "flex", alignItems: "center", justifyContent: "center", gap: "8px" }}>
                <ShieldCheck className="error-icon" style={{ flexShrink: 0 }} />
                <span>{error}</span>
              </div>
              <p style={{ fontSize: "13px", color: "#66736d", lineHeight: 1.6 }}>
                Please ask your administrator to send you a new invitation link.
              </p>
            </div>
          )}
        </div>
      </div>
    </main>
  );
}

export default function ActivatePage() {
  return (
    <Suspense fallback={<Loader variant="card" />}>
      <ActivateContent />
    </Suspense>
  );
}
