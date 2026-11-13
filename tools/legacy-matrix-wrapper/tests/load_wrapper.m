#import <AppKit/AppKit.h>
#import <ScreenSaver/ScreenSaver.h>

static void dumpView(NSView *view, NSInteger depth) {
    NSString *indent = [@"  " stringByPaddingToLength:(NSUInteger)depth * 2 withString:@"  " startingAtIndex:0];
    NSLog(@"%@%@ frame=%@ bounds=%@ backing=%@", indent, NSStringFromClass(view.class),
          NSStringFromRect(view.frame), NSStringFromRect(view.bounds),
          NSStringFromSize([view convertSizeToBacking:view.bounds.size]));
    for (NSView *child in view.subviews) dumpView(child, depth + 1);
}

int main(void) {
    @autoreleasepool {
        NSBundle *bundle = [NSBundle bundleWithPath:@[NSString stringWithFormat:@"%@/Library/Screen Savers/3D Matrix Code Fullscreen.saver", NSHomeDirectory()]];
        NSError *error = nil;
        if (![bundle loadAndReturnError:&error]) {
            NSLog(@"wrapper-load-failed: %@", error);
            return 1;
        }
        Class wrapperClass = NSClassFromString(@"MatrixCodeFullscreenView");
        if (!wrapperClass) return 2;
        ScreenSaverView *wrapper = [[wrapperClass alloc] initWithFrame:NSMakeRect(0, 0, 1728, 1117) isPreview:NO];
        if (wrapper.subviews.count != 2) return 3;
        if (![wrapper hasConfigureSheet] || [wrapper configureSheet] == nil) return 4;
        ScreenSaverView *legacy = [wrapper valueForKey:@"legacyView"];
        NSLog(@"legacy-options available=%d sheet=%@", legacy.hasConfigureSheet, legacy.configureSheet);
        dumpView(wrapper, 0);
    }
    return 0;
}
