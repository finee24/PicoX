import { AuthForm } from "@/components/auth-form";
import { AuthShell } from "@/components/auth-shell";

/**
 * `searchParams` è una Promise in Next.js 16: l'accesso sincrono, tollerato in
 * 15, è stato rimosso. Leggerla qui, in un Server Component, evita anche di
 * dover avvolgere `useSearchParams` in un boundary di Suspense.
 */
export default async function LoginPage(props: PageProps<"/login">) {
  const params = await props.searchParams;

  const redirectParam = params.redirect;
  const redirectTo = typeof redirectParam === "string" ? redirectParam : undefined;

  return (
    <AuthShell title="Accedi" description="Entra per vedere i tuoi insight.">
      <AuthForm mode="login" redirectTo={redirectTo} expired={params.expired === "1"} />
    </AuthShell>
  );
}
