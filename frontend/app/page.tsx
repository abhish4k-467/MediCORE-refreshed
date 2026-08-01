"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";
import { supabase } from "@/lib/supabase";
import Loader from "@/components/Loader";

export default function RootPage() {
  const router = useRouter();

  useEffect(() => {
    supabase.auth.getSession().then((res) => {
      const session = res?.data?.session;
      if (session?.user) {
        const role = session.user.user_metadata?.role;
        if (role === "admin") {
          router.replace("/admin");
        } else {
          router.replace("/employee");
        }
      } else {
        router.replace("/login");
      }
    }).catch(() => {
      router.replace("/login");
    });

  }, [router]);

  return <Loader variant="fullscreen" title="MediCORE" subtitle="Verifying your session..." />;
}
