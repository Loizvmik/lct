import type { Metadata } from "next";
import Link from "next/link";
import StepNav from "@/components/StepNav";
import "./globals.css";

export const metadata: Metadata = {
  title: "DeckForge",
  description: "Генерация презентаций по .pptx-шаблону",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="ru">
      <body>
        <div className="topbar">
          <div className="brand">
            Deck<span>Forge</span>
          </div>
          <StepNav />
          <Link className="button restart-button" href="/" title="Начать с чистого листа: новая загрузка шаблона и новый бриф">
            + Новая презентация
          </Link>
        </div>
        <div className="shell">{children}</div>
      </body>
    </html>
  );
}
