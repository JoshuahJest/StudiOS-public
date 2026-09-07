#import <AppKit/AppKit.h>
#import <ScreenSaver/ScreenSaver.h>

int main(void) {
    @autoreleasepool {
        NSBundle *bundle = [NSBundle bundleWithPath:@"/Users/thomas/Library/Screen Savers/3D Matrix Code Fullscreen.saver"];
        NSError *error = nil;
        if (![bundle loadAndReturnError:&error]) {
            NSLog(@"wrapper-load-failed: %@", error);
            return 1;
        }
        Class wrapperClass = NSClassFromString(@"MatrixCodeFullscreenView");
        if (!wrapperClass) return 2;
        ScreenSaverView *wrapper = [[wrapperClass alloc] initWithFrame:NSMakeRect(0, 0, 1728, 1117) isPreview:NO];
        if (wrapper.subviews.count != 1) return 3;
        NSView *legacy = wrapper.subviews.firstObject;
        NSLog(@"wrapper-load-ok frame=%@ bounds=%@", NSStringFromRect(legacy.frame), NSStringFromRect(legacy.bounds));
    }
    return 0;
}
