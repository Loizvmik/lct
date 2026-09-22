import type { Metadata } from "next";
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
          <div className="steps">
            <span className="step">1. Шаблон</span>
            <span className="step">2. Бриф</span>
            <span className="step">3. Варианты</span>
            <span className="step">4. Аудит</span>
          </div>
        </div>
        <div className="shell">{children}</div>
      </body>
    </html>
  );
}
