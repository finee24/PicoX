import { AuthForm } from "@/components/auth-form";
import { AuthShell } from "@/components/auth-shell";

export default function RegisterPage() {
  return (
    <AuthShell
      title="Crea un account"
      description="Bastano email e password per iniziare ad analizzare video."
    >
      <AuthForm mode="register" />
    </AuthShell>
  );
}
