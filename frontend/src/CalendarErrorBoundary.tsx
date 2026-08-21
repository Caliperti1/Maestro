import { Component, type ErrorInfo, type ReactNode } from "react";

type CalendarErrorBoundaryProps = {
  children: ReactNode;
  fallback: ReactNode;
  resetKey: string;
};

type CalendarErrorBoundaryState = {
  hasError: boolean;
};

export class CalendarErrorBoundary extends Component<
  CalendarErrorBoundaryProps,
  CalendarErrorBoundaryState
> {
  state: CalendarErrorBoundaryState = { hasError: false };

  static getDerivedStateFromError(): CalendarErrorBoundaryState {
    return { hasError: true };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error("Calendar grid failed to render", error, info);
  }

  componentDidUpdate(previousProps: CalendarErrorBoundaryProps) {
    if (this.state.hasError && previousProps.resetKey !== this.props.resetKey) {
      this.setState({ hasError: false });
    }
  }

  render() {
    return this.state.hasError ? this.props.fallback : this.props.children;
  }
}
