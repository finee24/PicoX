import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { PicoxMark } from "@/components/picox-mark";

/** Cornice condivisa da `/login` e `/register`. */
export function AuthShell({
  title,
  description,
  children,
}: {
  title: string;
  description: string;
  children: React.ReactNode;
}) {
  return (
    <main className="flex min-h-screen items-center justify-center px-4 py-12">
      <div className="w-full max-w-sm space-y-6">
        <div className="flex flex-col items-center gap-3 text-center">
          <PicoxMark className="size-11" />
          <div>
            <p className="text-xl font-semibold tracking-tight">Picox</p>
            <p className="text-muted-foreground text-sm">
              Insight riutilizzabili dai video brevi
            </p>
          </div>
        </div>

        <Card>
          <CardHeader>
            <CardTitle>{title}</CardTitle>
            <CardDescription>{description}</CardDescription>
          </CardHeader>
          <CardContent>{children}</CardContent>
        </Card>
      </div>
    </main>
  );
}
