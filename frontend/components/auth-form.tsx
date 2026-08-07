"use client";

import { useRouter } from "next/navigation";
import Link from "next/link";
import { useState } from "react";
import { Loader2, MailCheck } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { safeInternalPath } from "@/lib/safe-redirect";
import { getSupabaseBrowserClient } from "@/lib/supabase-client";

type Mode = "login" | "register";

interface AuthFormProps {
  mode: Mode;
  /** Percorso a cui tornare dopo l'accesso, propagato da `proxy.ts`. */
  redirectTo?: string;
  /** `true` quando si arriva qui perché la sessione è scaduta. */
  expired?: boolean;
}

/**
 * I messaggi di GoTrue arrivano in inglese e con formulazioni che non hanno
 * senso per un utente finale. Si traducono i casi ricorrenti e si lascia
 * passare il resto, invece di mostrare un generico "errore" che nasconde
 * informazioni utili.
 */
function translateAuthError(message: string): string {
  const normalized = message.toLowerCase();

  if (normalized.includes("invalid login credentials")) {
    return "Email o password non corretti.";
  }
  if (normalized.includes("email not confirmed")) {
    return "Devi prima confermare l'indirizzo email. Controlla la posta.";
  }
  if (normalized.includes("user already registered")) {
    return "Esiste già un account con questa email. Prova ad accedere.";
  }
  if (normalized.includes("password should be at least")) {
    return "La password è troppo corta: servono almeno 6 caratteri.";
  }
  if (normalized.includes("unable to validate email address")) {
    return "L'indirizzo email non sembra valido.";
  }
  if (normalized.includes("rate limit") || normalized.includes("too many requests")) {
    return "Troppi tentativi. Attendi qualche minuto e riprova.";
  }
  if (normalized.includes("fetch") || normalized.includes("network")) {
    return "Impossibile raggiungere Supabase. Verifica la connessione.";
  }
  return message;
}

export function AuthForm({ mode, redirectTo, expired }: AuthFormProps) {
  const router = useRouter();
  const isRegister = mode === "register";

  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);
  const [confirmationSent, setConfirmationSent] = useState(false);

  async function handleSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);
    setPending(true);

    const supabase = getSupabaseBrowserClient();

    try {
      if (isRegister) {
        const { data, error: signUpError } = await supabase.auth.signUp({
          email,
          password,
          options: {
            emailRedirectTo: `${window.location.origin}/auth/callback`,
          },
        });

        if (signUpError) {
          setError(translateAuthError(signUpError.message));
          return;
        }

        // Con la conferma via email attiva, `session` è null finché l'utente
        // non clicca il link: non c'è nessuna dashboard da mostrare ancora.
        if (!data.session) {
          setConfirmationSent(true);
          return;
        }
      } else {
        const { error: signInError } = await supabase.auth.signInWithPassword({
          email,
          password,
        });

        if (signInError) {
          setError(translateAuthError(signInError.message));
          return;
        }
      }

      // `refresh()` prima di `push()`: rilegge i cookie di sessione lato server,
      // altrimenti il proxy vedrebbe ancora lo stato precedente e rimanderebbe
      // al login.
      router.refresh();
      // La destinazione arriva dalla query string: va normalizzata, altrimenti
      // `?redirect=//evil.com` porterebbe fuori dominio proprio dopo un login
      // riuscito. Vedi `lib/safe-redirect.ts`.
      router.push(safeInternalPath(redirectTo));
    } catch (caught) {
      // Eccezione non prevista: il dettaglio resta in console, all'utente va un
      // messaggio nostro. `translateAuthError` fa da passthrough sui casi non
      // riconosciuti, e qui non sappiamo cosa contenga il testo originale.
      console.error("Errore inatteso durante l'autenticazione:", caught);
      setError("Si è verificato un errore imprevisto. Riprova.");
    } finally {
      setPending(false);
    }
  }

  if (confirmationSent) {
    return (
      <div className="space-y-4 text-center">
        <div className="mx-auto flex size-12 items-center justify-center rounded-full bg-emerald-500/10">
          <MailCheck className="size-6 text-emerald-400" aria-hidden />
        </div>
        <div className="space-y-1">
          <h2 className="text-lg font-medium">Controlla la tua email</h2>
          <p className="text-muted-foreground text-sm">
            Abbiamo inviato un link di conferma a{" "}
            <span className="text-foreground font-medium">{email}</span>. Aprilo per
            attivare l&apos;account.
          </p>
        </div>
        <Button variant="outline" className="w-full" asChild>
          <Link href="/login">Torna all&apos;accesso</Link>
        </Button>
      </div>
    );
  }

  return (
    <form onSubmit={handleSubmit} className="space-y-4" noValidate>
      {expired && !error && (
        <p
          role="status"
          className="rounded-md border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-sm text-amber-300"
        >
          La sessione è scaduta. Accedi di nuovo per continuare.
        </p>
      )}

      <div className="space-y-2">
        <Label htmlFor="email">Email</Label>
        <Input
          id="email"
          name="email"
          type="email"
          autoComplete="email"
          required
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          placeholder="tu@esempio.it"
          disabled={pending}
        />
      </div>

      <div className="space-y-2">
        <Label htmlFor="password">Password</Label>
        <Input
          id="password"
          name="password"
          type="password"
          autoComplete={isRegister ? "new-password" : "current-password"}
          required
          minLength={6}
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          placeholder={isRegister ? "Almeno 6 caratteri" : "••••••••"}
          disabled={pending}
        />
      </div>

      {error && (
        <p
          role="alert"
          className="rounded-md border border-red-500/30 bg-red-500/10 px-3 py-2 text-sm text-red-300"
        >
          {error}
        </p>
      )}

      <Button type="submit" className="w-full" disabled={pending}>
        {pending && <Loader2 className="size-4 animate-spin" aria-hidden />}
        {isRegister ? "Crea account" : "Accedi"}
      </Button>

      <p className="text-muted-foreground text-center text-sm">
        {isRegister ? (
          <>
            Hai già un account?{" "}
            <Link href="/login" className="text-emerald-400 underline-offset-4 hover:underline">
              Accedi
            </Link>
          </>
        ) : (
          <>
            Non hai un account?{" "}
            <Link
              href="/register"
              className="text-emerald-400 underline-offset-4 hover:underline"
            >
              Registrati
            </Link>
          </>
        )}
      </p>
    </form>
  );
}
