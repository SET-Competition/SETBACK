import type { Metadata } from "next";
import RegisterForm from "./register-form";

/**
 * A thin server component so this route can carry its own <title>. The form
 * itself is a client component — it holds submit state and talks to Supabase
 * from the browser — and a "use client" module cannot export `metadata`.
 */
export const metadata: Metadata = {
  title: "SET — Register",
  description:
    "Register read-only exchange credentials for the SET trading competition.",
};

export default function RegisterPage() {
  return <RegisterForm />;
}
