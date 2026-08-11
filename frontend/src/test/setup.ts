import '@testing-library/jest-dom';

// antd v5 依赖 window.matchMedia（响应式断点），jsdom 未实现。
if (typeof window !== 'undefined' && typeof window.matchMedia !== 'function') {
  Object.defineProperty(window, 'matchMedia', {
    writable: true,
    value: (query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addListener: () => {},
      removeListener: () => {},
      addEventListener: () => {},
      removeEventListener: () => {},
      dispatchEvent: () => false,
    }),
  });
}

// ReportTab「定位报告」调用 scrollIntoView；jsdom 未实现，stub 掉避免报错。
if (typeof Element !== 'undefined' && typeof Element.prototype.scrollIntoView !== 'function') {
  Element.prototype.scrollIntoView = () => {};
}

// antd 某些组件使用 getComputedStyle + ResizeObserver。
if (typeof window !== 'undefined' && typeof window.ResizeObserver !== 'function') {
  class ResizeObserverStub {
    observe(): void {}
    unobserve(): void {}
    disconnect(): void {}
  }
  Object.defineProperty(window, 'ResizeObserver', {
    writable: true,
    value: ResizeObserverStub,
  });
}
