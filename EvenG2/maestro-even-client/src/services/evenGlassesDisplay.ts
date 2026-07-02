import {
  CreateStartUpPageContainer,
  OsEventTypeList,
  StartUpPageCreateResult,
  TextContainerProperty,
  TextContainerUpgrade,
  waitForEvenAppBridge,
} from "@evenrealities/even_hub_sdk";

const CONTAINER_ID = 1;
const CONTAINER_NAME = "maestro-main";

type GestureHandlers = {
  onSingleTap?: () => void | Promise<void>;
  onDoubleTap?: () => void | Promise<void>;
  onSwipeUp?: () => void | Promise<void>;
  onSwipeDown?: () => void | Promise<void>;
};

async function withTimeout<T>(promise: Promise<T>, timeoutMs: number): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const timer = window.setTimeout(() => reject(new Error("Even bridge timeout.")), timeoutMs);
    promise
      .then((result) => {
        window.clearTimeout(timer);
        resolve(result);
      })
      .catch((error) => {
        window.clearTimeout(timer);
        reject(error);
      });
  });
}

export class EvenGlassesDisplay {
  private initialized = false;

  private supported = true;

  private gestureBound = false;

  private handlers: GestureHandlers = {};

  async ensureReady(): Promise<boolean> {
    if (this.initialized) return true;
    if (!this.supported) return false;

    try {
      const bridge = await withTimeout(waitForEvenAppBridge(), 3000);
      const result = await bridge.createStartUpPageContainer(
        new CreateStartUpPageContainer({
          containerTotalNum: 1,
          textObject: [
            new TextContainerProperty({
              xPosition: 0,
              yPosition: 0,
              width: 576,
              height: 288,
              borderWidth: 0,
              borderColor: 5,
              paddingLength: 2,
              containerID: CONTAINER_ID,
              containerName: CONTAINER_NAME,
              content: "Maestro EvenG2 connected.",
              isEventCapture: 1,
            }),
          ],
        }),
      );

      if (result !== StartUpPageCreateResult.success) {
        this.supported = false;
        return false;
      }

      this.initialized = true;
      return true;
    } catch {
      this.supported = false;
      return false;
    }
  }

  async renderText(text: string): Promise<void> {
    const ready = await this.ensureReady();
    if (!ready) return;

    const bridge = await waitForEvenAppBridge();
    const safeText = text.slice(0, 950);
    await bridge.textContainerUpgrade(
      new TextContainerUpgrade({
        containerID: CONTAINER_ID,
        containerName: CONTAINER_NAME,
        content: safeText,
      }),
    );
  }

  async onGestures(handlers: GestureHandlers): Promise<void> {
    const ready = await this.ensureReady();
    if (!ready) return;

    this.handlers = handlers;
    if (this.gestureBound) return;

    const bridge = await waitForEvenAppBridge();
    bridge.onEvenHubEvent((event) => {
      const textEventType =
        event.textEvent?.containerID === CONTAINER_ID ? event.textEvent.eventType : undefined;
      const eventType = textEventType ?? event.sysEvent?.eventType;
      if (eventType === undefined) return;

      if (eventType === OsEventTypeList.CLICK_EVENT) {
        Promise.resolve(this.handlers.onSingleTap?.()).catch(() => {});
        return;
      }
      if (eventType === OsEventTypeList.DOUBLE_CLICK_EVENT) {
        Promise.resolve(this.handlers.onDoubleTap?.()).catch(() => {});
        return;
      }
      if (eventType === OsEventTypeList.SCROLL_TOP_EVENT) {
        Promise.resolve(this.handlers.onSwipeUp?.()).catch(() => {});
        return;
      }
      if (eventType === OsEventTypeList.SCROLL_BOTTOM_EVENT) {
        Promise.resolve(this.handlers.onSwipeDown?.()).catch(() => {});
      }
    });

    this.gestureBound = true;
  }
}

export const evenGlassesDisplay = new EvenGlassesDisplay();
