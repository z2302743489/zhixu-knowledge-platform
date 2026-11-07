import React from 'react';
import { createRoot } from 'react-dom/client';
import { App as AntApp, ConfigProvider } from 'antd';
import zhCN from 'antd/locale/zh_CN';
import Platform from './Platform';

createRoot(document.getElementById('root')!).render(<ConfigProvider locale={zhCN} theme={{ token: { colorPrimary: '#7562d8', borderRadius: 8, fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif', colorText: '#303345', colorTextSecondary: '#8a8c9c', colorBorder: '#e7e7ef', controlHeight: 36 }, components: { Button: { primaryShadow: '0 3px 8px rgba(117,98,216,.14)' }, Table: { headerBg: '#fafafd', headerColor: '#86889a', cellPaddingBlock: 16 } } }}><AntApp><Platform /></AntApp></ConfigProvider>);
