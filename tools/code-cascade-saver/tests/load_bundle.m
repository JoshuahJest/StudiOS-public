#import <AppKit/AppKit.h>

int main(void) {
    @autoreleasepool {
        NSBundle *bundle = [NSBundle bundleWithPath:@"/Users/thomas/Library/Screen Savers/Code Cascade.saver"];
        NSError *error = nil;
        if (![bundle loadAndReturnError:&error]) {
            NSLog(@"bundle-load-failed: %@", error);
            return 1;
        }
        Class saverClass = NSClassFromString(@"CodeCascadeSaverView");
        if (saverClass == Nil) {
            NSLog(@"principal-class-missing");
            return 2;
        }
        NSLog(@"bundle-load-ok: %@", saverClass);
    }
    return 0;
}
