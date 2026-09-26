import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  devIndicators: false,
  // Next 16 по умолчанию отдаёт свои dev-ресурсы (в том числе горячую
  // перезагрузку) только хосту, под которым сам запущен, — страница,
  // открытая по 127.0.0.1, получает `Blocked cross-origin request to
  // Next.js dev resource`, клиентский код не грузится, и кнопки на
  // странице просто ничего не делают. Молча: в браузере ошибка видна
  // только в консоли.
  //
  // `scripts/run.sh` печатает адрес с 127.0.0.1, и первый же запуск
  // упирается ровно в это. Оба написания одного и того же локального
  // адреса разрешены явно.
  allowedDevOrigins: ["127.0.0.1", "localhost"],
};

export default nextConfig;
